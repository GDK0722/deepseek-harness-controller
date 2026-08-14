#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""DSH 本地用量聚合器 —— 读取 ~/.dsh/sessions 会话日志，对齐平台用量维度。

数据源：session.jsonl.zstd（zstd 压缩的 JSONL，每行一个事件）
- usage 记录：assistant/chunk 事件，chunk.type == "usage"
- 模型关联：最近的 request/header、session/title-llm-request、
  web/deepseek-search-llm-request 事件中的 model 字段
- 时间戳：事件 time 字段（epoch 毫秒）

价格表：同目录 pricing.json（人民币 / 百万 tokens，含峰谷价规则）；
DeepSeek 改价后更新 pricing.json 即可，无需改代码。pricing.json 缺失
或损坏时使用代码内置默认值（2026-08-14 官方定价）。
"""

import json
import shutil
import subprocess
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

SESSIONS_DIR = Path.home() / ".dsh" / "sessions"
CACHE_FILE = Path.home() / ".dsh" / "controller_usage_cache.json"
# zstd 可能在不同位置（Apple Silicon / Intel Homebrew 或 PATH 中）
ZSTD = (shutil.which("zstd")
        or next((p for p in ("/opt/homebrew/bin/zstd", "/usr/local/bin/zstd")
                 if Path(p).exists()), None))

BJT = timezone(timedelta(hours=8))
PEAK_SWITCH_TS = datetime(2026, 8, 17, 0, 0, tzinfo=BJT).timestamp() * 1000

# (命中, 未命中, 输出) 元 / 百万 tokens —— 内置默认值，
# 同目录 pricing.json 存在时会覆盖（DeepSeek 改价后只需更新该文件）
PRICE_FLAT = {
    "deepseek-v4-flash": (0.02, 1.0, 2.0),
    "deepseek-v4-pro": (0.025, 3.0, 6.0),
}
PRICE_PEAK = {
    "deepseek-v4-flash": (0.10, 3.0, 9.0),
    "deepseek-v4-pro": (0.30, 9.0, 27.0),
}
PRICE_OFFPEAK = {
    "deepseek-v4-flash": (0.05, 1.5, 4.5),
    "deepseek-v4-pro": (0.15, 4.5, 13.5),
}
DEFAULT_MODEL = "deepseek-v4-pro"
PEAK_HOURS = [(9, 12), (14, 18)]  # 北京时间高峰时段


def _load_pricing():
    """从脚本同目录的 pricing.json 读取价格表；读取失败则保留内置默认值。"""
    global PEAK_SWITCH_TS, DEFAULT_MODEL, PEAK_HOURS
    try:
        cfg = json.loads(
            (Path(__file__).parent / "pricing.json").read_text())
        DEFAULT_MODEL = cfg.get("default_model", DEFAULT_MODEL)
        PEAK_HOURS = [tuple(x) for x in cfg.get("peak_hours_bjt", PEAK_HOURS)]
        eff = cfg.get("peak_pricing_effective_from")
        if eff:
            PEAK_SWITCH_TS = datetime.fromisoformat(eff).timestamp() * 1000
        for name, tables in cfg.get("models", {}).items():
            if "flat" in tables:
                PRICE_FLAT[name] = tuple(tables["flat"])
            if "peak" in tables:
                PRICE_PEAK[name] = tuple(tables["peak"])
            if "offpeak" in tables:
                PRICE_OFFPEAK[name] = tuple(tables["offpeak"])
    except Exception:
        pass


_load_pricing()


def _is_peak(ts_ms):
    dt = datetime.fromtimestamp(ts_ms / 1000, tz=BJT)
    return any(lo <= dt.hour < hi for lo, hi in PEAK_HOURS)


def price_for(model, ts_ms):
    if ts_ms < PEAK_SWITCH_TS:
        return PRICE_FLAT.get(model, PRICE_FLAT[DEFAULT_MODEL])
    table = PRICE_PEAK if _is_peak(ts_ms) else PRICE_OFFPEAK
    return table.get(model, table[DEFAULT_MODEL])


def extract_model(obj):
    """从事件中提取模型名，没有则返回 None。"""
    t = obj.get("type")
    d = obj.get("data") or {}
    if t == "request/header":
        return (d.get("header") or {}).get("config", {}).get("model")
    if t == "session/title-llm-request":
        return (d.get("route") or {}).get("model")
    if t == "web/deepseek-search-llm-request":
        return (d.get("body") or {}).get("model")
    return None


def parse_session_file(path):
    """解析单个会话文件，返回 [(ts_ms, model, in, out, cache), ...]（每元素=一次请求）。"""
    if ZSTD is None:
        return []  # 未安装 zstd（brew install zstd）
    try:
        r = subprocess.run([ZSTD, "-dc", str(path)],
                           capture_output=True, timeout=60)
        text = r.stdout.decode("utf-8", errors="replace")
    except Exception:
        return []
    records = []
    model = None
    for line in text.splitlines():
        if '"usage"' not in line and '"model"' not in line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        m = extract_model(obj)
        if m:
            model = m
            continue
        if obj.get("type") != "assistant/chunk":
            continue
        chunk = (obj.get("data") or {}).get("chunk") or {}
        if chunk.get("type") != "usage":
            continue
        u = chunk.get("usage") or {}
        records.append((
            obj.get("time", 0),
            model or DEFAULT_MODEL,
            int(u.get("inputTokens", 0)),
            int(u.get("outputTokens", 0)),
            int(u.get("cacheReadTokens", 0)),
        ))
    return records


def collect_records():
    """收集全部会话的用量记录，带 mtime+size 缓存。"""
    cache = {}
    if CACHE_FILE.exists():
        try:
            cache = json.loads(CACHE_FILE.read_text())
        except Exception:
            cache = {}
    new_cache = {}
    all_records = []
    if SESSIONS_DIR.is_dir():
        for path in SESSIONS_DIR.glob("*/session-*/session.jsonl.zstd"):
            try:
                st = path.stat()
            except OSError:
                continue
            key = str(path)
            sig = [st.st_mtime, st.st_size]
            hit = cache.get(key)
            if hit and hit.get("sig") == sig:
                records = [tuple(r) for r in hit["records"]]
            else:
                records = parse_session_file(path)
            new_cache[key] = {"sig": sig,
                              "records": [list(r) for r in records]}
            all_records.extend(records)
    try:
        CACHE_FILE.write_text(json.dumps(new_cache))
    except Exception:
        pass
    return all_records


def _blank_bucket():
    return {"requests": 0, "input": 0, "output": 0, "cache": 0, "cost": 0.0}


def _add(bucket, rec):
    ts, model, inp, out, cache = rec
    hit_p, miss_p, out_p = price_for(model, ts)
    bucket["requests"] += 1
    bucket["input"] += inp
    bucket["output"] += out
    bucket["cache"] += cache
    bucket["cost"] += (cache * hit_p + inp * miss_p + out * out_p) / 1_000_000


def aggregate(records, now=None):
    """聚合为平台页面对齐的统计结构。"""
    now = now or time.time()
    now_dt = datetime.fromtimestamp(now, tz=BJT)
    today_start = now_dt.replace(hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000
    month_start = now_dt.replace(day=1, hour=0, minute=0, second=0,
                                 microsecond=0).timestamp() * 1000

    today, month = _blank_bucket(), _blank_bucket()
    by_model = {}
    by_day = {}
    by_day_model = {}

    for rec in records:
        ts, model = rec[0], rec[1]
        if ts >= month_start:
            _add(month, rec)
        if ts >= today_start:
            _add(today, rec)
        bm = by_model.setdefault(model, _blank_bucket())
        _add(bm, rec)
        day = datetime.fromtimestamp(ts / 1000, tz=BJT).strftime("%m-%d")
        bd = by_day.setdefault(day, _blank_bucket())
        _add(bd, rec)
        bdm = by_day_model.setdefault(day, {}).setdefault(model, _blank_bucket())
        _add(bdm, rec)

    # 近 7 天（含今天），按日期排序补齐空缺；每天带分模型明细
    last7 = []
    for i in range(6, -1, -1):
        d = (now_dt - timedelta(days=i)).strftime("%m-%d")
        b = by_day.get(d, _blank_bucket())
        models = {}
        for m, mb in sorted(by_day_model.get(d, {}).items()):
            models[m] = {"cost": round(mb["cost"], 4),
                         "tokens": mb["input"] + mb["output"] + mb["cache"]}
        last7.append({"day": d, "cost": round(b["cost"], 4),
                      "tokens": b["input"] + b["output"] + b["cache"],
                      "models": models})

    def finalize(b):
        total = b["input"] + b["output"] + b["cache"]
        hit_rate = (b["cache"] / (b["cache"] + b["input"]) * 100
                    if (b["cache"] + b["input"]) else 0.0)
        return {**b, "total_tokens": total, "cache_hit_rate": round(hit_rate, 1),
                "cost": round(b["cost"], 4)}

    return {
        "today": finalize(today),
        "month": finalize(month),
        "by_model": {m: finalize(b) for m, b in sorted(by_model.items())},
        "last7": last7,
        "generated_at": now_dt.strftime("%H:%M:%S"),
    }


def fetch_balance(api_key):
    """官方余额接口。返回 (ok, total_balance_str)。"""
    try:
        r = subprocess.run(
            ["/usr/bin/curl", "-s", "--max-time", "8",
             "-H", f"Authorization: Bearer {api_key}",
             "https://api.deepseek.com/user/balance"],
            capture_output=True, text=True, timeout=12)
        data = json.loads(r.stdout)
        infos = data.get("balance_infos") or []
        if infos:
            return True, infos[0].get("total_balance", "?")
    except Exception:
        pass
    return False, None


def load_api_key():
    try:
        for line in (Path.home() / ".dsh" / ".credentials.yaml").read_text().splitlines():
            if line.startswith("DEEPSEEK_API_KEY:"):
                return line.split(":", 1)[1].strip()
    except Exception:
        pass
    return None


if __name__ == "__main__":
    recs = collect_records()
    print(f"记录数: {len(recs)}")
    agg = aggregate(recs)
    print(json.dumps(agg, ensure_ascii=False, indent=2))
    key = load_api_key()
    ok, bal = fetch_balance(key) if key else (False, None)
    print(f"余额: {'¥' + bal if ok else '获取失败'}")
