#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""DSH Controller — DeepSeek Harness 服务控制器 + 用量统计

Coded by Kimi and DK

v7：渲染层迁移到原生 Cocoa（pyobjc → AppKit）。
Tk 在 Retina 屏上把位图按 1pt=1图像像素放大渲染导致模糊（v6 教训），
Canvas 矢量绘制又无抗锯齿（v5 教训）。Cocoa 的 NSBezierPath /
NSString 绘制天然 Retina-sharp + 全抗锯齿，彻底解决。

UI 风格对齐 DeepSeek 开放平台（platform.deepseek.com）：
- 底色 #151517、圆角卡片 #2E2F31、文字 #F9FAFB、DeepSeek 蓝 #1B7DF1
- 卡片 = 标签在上、大号数字在下、灰色 CNY 后缀；胶囊按钮；灰胶囊选项卡
- 近 7 天图表支持 金额 / Tokens 双维度切换

线程模型：工作线程只做 subprocess 探测/聚合，结果进 queue；
NSTimer 在主线程轮询 queue 并刷新视图。
"""

import os
import sys

# pyobjc 安装在项目本地 vendor 目录（不污染托管 Python 环境）
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "vendor"))

import queue
import subprocess
import threading
import time
import traceback

import objc
import AppKit as A
import Foundation as F

import usage_stats

LABEL = "com.deepseek.harness.web"
TOPUP_URL = "https://platform.deepseek.com/top_up"
PLIST = os.path.expanduser(f"~/Library/LaunchAgents/{LABEL}.plist")
DEBUG_LOG = "/tmp/dsh-controller.log"


def _load_port():
    """DSH Web UI 端口：默认 3080；~/.dsh/controller.json 里可覆盖
    （{"port": 8080}），供使用 `dsh web --port` 的非默认安装。"""
    try:
        import json
        from pathlib import Path
        cfg = json.loads(
            (Path.home() / ".dsh" / "controller.json").read_text())
        return int(cfg.get("port", 3080))
    except Exception:
        return 3080


DSH_PORT = _load_port()
PROBE_URL = f"http://127.0.0.1:{DSH_PORT}/"

CURL = "/usr/bin/curl"
PGREP = "/usr/bin/pgrep"
PS = "/bin/ps"
LAUNCHCTL = "/bin/launchctl"
OPEN = "/usr/bin/open"

# ── DeepSeek 官方风格色板（采样自 platform.deepseek.com）──
BG = "#151517"
BG_PANEL = "#1E1F24"
BG_CARD = "#2E2F31"
FG = "#F9FAFB"
FG_DIM = "#9CA3AF"
FG_CREDIT = "#4B5058"
ACCENT = "#1B7DF1"
ACCENT_HOVER = "#3B8DF4"
ACCENT_PRESS = "#1565D8"
BAR_TOP = "#55A3F8"
WHITE_BTN = "#F8F9FA"

# 分模型堆叠柱配色（底部色, 顶部色）
MODEL_COLORS = {
    "deepseek-v4-flash": (ACCENT, BAR_TOP),
    "deepseek-v4-pro": ("#8B5CF6", "#C4B5FD"),
}
MODEL_COLOR_OTHER = ("#5B6068", "#9CA3AF")
GREEN = "#22C55E"
RED = "#EF4444"
GRAY = "#5B6068"
ORANGE = "#F5A623"
BTN_BG = "#2E2F31"
BTN_BG_HOVER = "#3A3D41"
BTN_BG_PRESS = "#24262B"
BTN_BG_OFF = "#1E1F24"
BTN_FG_OFF = "#565B63"
TAB_ON_BG = "#3A3D41"

WIN_W = 368


def C(hexstr):
    h = hexstr.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) / 255.0 for i in (0, 2, 4))
    return A.NSColor.colorWithCalibratedRed_green_blue_alpha_(r, g, b, 1.0)


def f_sys(size, semibold=False):
    w = A.NSFontWeightSemibold if semibold else A.NSFontWeightRegular
    return A.NSFont.systemFontOfSize_weight_(size, w)


def f_num(size):
    return (A.NSFont.fontWithName_size_("HelveticaNeue-Bold", size)
            or A.NSFont.boldSystemFontOfSize_(size))


def f_mono(size):
    return (A.NSFont.fontWithName_size_("Menlo", size)
            or A.NSFont.systemFontOfSize_(size))


def ns(s):
    """Python str → NSString（PyObjC 不再给 str 代理 NSString 方法）。"""
    return F.NSString.stringWithString_(s)


def draw_text(s, x, y, font, color):
    ns(s).drawAtPoint_withAttributes_(
        F.NSMakePoint(x, y),
        {A.NSFontAttributeName: font,
         A.NSForegroundColorAttributeName: color})


def text_w(s, font):
    return ns(s).sizeWithAttributes_({A.NSFontAttributeName: font}).width


def draw_text_right(s, right_x, y, font, color):
    draw_text(s, right_x - text_w(s, font), y, font, color)


def draw_text_center(s, cx, y, font, color):
    draw_text(s, cx - text_w(s, font) / 2, y, font, color)


def safe_draw(fn):
    """drawRect 防护：绘制异常只记日志，不再让 ObjC 异常炸掉整个 App。"""
    def wrapper(self, rect):
        try:
            fn(self, rect)
        except Exception:
            debug("drawRect error:\n" + traceback.format_exc())
    wrapper.__name__ = fn.__name__
    return wrapper


class V(A.NSView):
    """flipped 坐标系基类（y 轴向下）。"""

    def isFlipped(self):
        return True


# ════════════════════════════════════════════════════════════
#  控件
# ════════════════════════════════════════════════════════════

class PillButton(V):
    """胶囊按钮：原生绘制 4 态（常态/悬停/按下/禁用），Retina 锐利。"""

    def initWithFrame_(self, frame):
        self = objc.super(PillButton, self).initWithFrame_(frame)
        if self is None:
            return None
        self.text = ""
        self.cmd = None
        self.c_bg, self.c_fg = BTN_BG, FG
        self.c_hover, self.c_press = BTN_BG_HOVER, BTN_BG_PRESS
        self.font_size = 12
        self.enabled = True
        self.hover = False
        self.pressed = False
        ta = A.NSTrackingArea.alloc().initWithRect_options_owner_userInfo_(
            F.NSZeroRect,
            A.NSTrackingMouseEnteredAndExited | A.NSTrackingActiveAlways
            | A.NSTrackingInVisibleRect,
            self, None)
        self.addTrackingArea_(ta)
        return self

    def setupWithText_command_colors_fontSize_(self, text, cmd,
                                               colors=None, font_size=None):
        self.text = text
        self.cmd = cmd
        if colors:
            self.c_bg, self.c_fg, self.c_hover, self.c_press = colors
        if font_size:
            self.font_size = font_size
        self.setNeedsDisplay_(True)

    def setEnabled_(self, flag):
        self.enabled = bool(flag)
        self.setNeedsDisplay_(True)

    def setText_(self, text):
        self.text = text
        self.setNeedsDisplay_(True)

    def setColors_(self, colors):
        self.c_bg, self.c_fg, self.c_hover, self.c_press = colors
        self.setNeedsDisplay_(True)

    @safe_draw
    def drawRect_(self, _rect):
        if not self.enabled:
            fill, fg = BTN_BG_OFF, BTN_FG_OFF
        elif self.pressed:
            fill, fg = self.c_press, self.c_fg
        elif self.hover:
            fill, fg = self.c_hover, self.c_fg
        else:
            fill, fg = self.c_bg, self.c_fg
        b = self.bounds()
        path = A.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
            b, b.size.height / 2, b.size.height / 2)
        C(fill).set()
        path.fill()
        font = f_sys(self.font_size, semibold=True)
        h = ns(self.text).sizeWithAttributes_(
            {A.NSFontAttributeName: font}).height
        draw_text_center(self.text, b.size.width / 2,
                         (b.size.height - h) / 2 - 1, font, C(fg))

    def mouseEntered_(self, _e):
        self.hover = True
        self.setNeedsDisplay_(True)
        (A.NSCursor.pointingHandCursor() if self.enabled
         else A.NSCursor.arrowCursor()).set()

    def mouseExited_(self, _e):
        self.hover = False
        self.pressed = False
        self.setNeedsDisplay_(True)
        A.NSCursor.arrowCursor().set()

    def mouseDown_(self, _e):
        if self.enabled:
            self.pressed = True
            self.setNeedsDisplay_(True)

    def mouseUp_(self, e):
        was = self.pressed
        self.pressed = False
        self.setNeedsDisplay_(True)
        pt = self.convertPoint_fromView_(e.locationInWindow(), None)
        if was and self.enabled and A.NSPointInRect(pt, self.bounds()):
            if self.cmd:
                self.cmd()


class MetricCard(V):
    """圆角指标卡：标签在上、大数字在下、可选单位后缀、可选整卡点击。"""

    def initWithFrame_title_valueSize_onClick_showPill_(
            self, frame, title, vs, on_click, pill):
        self = objc.super(MetricCard, self).initWithFrame_(frame)
        if self is None:
            return None
        self.title = title
        self.vs = vs
        self.value = "—"
        self.unit = None
        self.on_click = on_click
        self.show_pill = pill
        return self

    def setValue_unit_(self, value, unit):
        self.value = value
        self.unit = unit
        self.setNeedsDisplay_(True)

    @safe_draw
    def drawRect_(self, _rect):
        b = self.bounds()
        path = A.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
            b, 12, 12)
        C(BG_CARD).set()
        path.fill()
        draw_text(self.title, 12, 13, f_sys(9), C(FG_DIM))
        # 大数字 + 单位后缀（底部基线对齐）
        vf = f_num(self.vs)
        vh = ns(self.value).sizeWithAttributes_(
            {A.NSFontAttributeName: vf}).height
        by = b.size.height - 12 - vh
        draw_text(self.value, 12, by, vf, C(FG))
        if self.unit:
            x = 12 + text_w(self.value, vf) + 5
            uf = f_sys(9)
            uh = ns(self.unit).sizeWithAttributes_(
                {A.NSFontAttributeName: uf}).height
            draw_text(self.unit, x, b.size.height - 12 - uh, uf, C(FG_DIM))
        # 内嵌「去充值」白胶囊（整卡可点，pill 仅视觉）
        if self.show_pill:
            pr = F.NSMakeRect(b.size.width - 68, 10, 58, 24)
            pp = A.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
                pr, 12, 12)
            C(WHITE_BTN).set()
            pp.fill()
            draw_text_center("去充值", pr.origin.x + 29, pr.origin.y + 5,
                             f_sys(9, semibold=True), C("#0F1115"))

    def resetCursorRects(self):
        if self.on_click:
            self.addCursorRect_cursor_(self.bounds(),
                                       A.NSCursor.pointingHandCursor())

    def mouseUp_(self, e):
        if not self.on_click:
            return
        pt = self.convertPoint_fromView_(e.locationInWindow(), None)
        if A.NSPointInRect(pt, self.bounds()):
            self.on_click()


class StatusCard(V):
    """服务状态卡：状态灯 + 状态文字 + 详情行。"""

    def initWithFrame_(self, frame):
        self = objc.super(StatusCard, self).initWithFrame_(frame)
        if self is None:
            return None
        self.color = ORANGE
        self.text = "检测中…"
        self.detail = ""
        return self

    def renderState_pid_uptime_(self, state, pid, uptime):
        self.color = {"running": GREEN, "error": RED,
                      "stopped": GRAY, "checking": ORANGE}[state]
        self.text = {"running": "运行正常", "error": "异常：进程在，但服务无响应",
                     "stopped": "未启动", "checking": "检测中…"}[state]
        if pid:
            self.detail = f"PID {pid} · 端口 {DSH_PORT}"
        else:
            self.detail = f"服务未在运行 · 端口 {DSH_PORT}"
        self.setNeedsDisplay_(True)

    @safe_draw
    def drawRect_(self, _rect):
        b = self.bounds()
        path = A.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
            b, 12, 12)
        C(BG_CARD).set()
        path.fill()
        dot = A.NSBezierPath.bezierPathWithOvalInRect_(
            F.NSMakeRect(14, b.size.height / 2 - 8, 16, 16))
        C(self.color).set()
        dot.fill()
        draw_text(self.text, 42, 14, f_sys(14, semibold=True), C(FG))
        draw_text(self.detail, 42, b.size.height - 26, f_mono(10),
                  C(FG_DIM))


class ModelLines(V):
    """按模型分行（Menlo，CJK 自动级联到苹方，不会豆腐块）。"""

    def initWithFrame_(self, frame):
        self = objc.super(ModelLines, self).initWithFrame_(frame)
        if self is not None:
            self.lines = []
        return self

    def setLines_(self, lines):
        self.lines = list(lines)
        self.setNeedsDisplay_(True)

    @safe_draw
    def drawRect_(self, _rect):
        for i, line in enumerate(self.lines[:3]):
            draw_text(line, 2, 2 + i * 16, f_mono(10), C(FG_DIM))


class ModelShareCard(V):
    """分模型占比卡：Tokens / 金额 两条横向堆叠条（flash 蓝 / pro 紫）。

    setShareRows_ 接收 [(label, [(model, value)], total_str), ...]
    """

    def initWithFrame_(self, frame):
        self = objc.super(ModelShareCard, self).initWithFrame_(frame)
        if self is None:
            return None
        self.rows = []
        self.models = []
        self.details = {}
        self.hover = None  # (row_i, model)
        ta = A.NSTrackingArea.alloc().initWithRect_options_owner_userInfo_(
            F.NSZeroRect,
            A.NSTrackingMouseEnteredAndExited | A.NSTrackingMouseMoved
            | A.NSTrackingActiveAlways | A.NSTrackingInVisibleRect,
            self, None)
        self.addTrackingArea_(ta)
        return self

    def setShareRows_models_details_(self, rows, models, details):
        self.rows = rows
        self.models = models
        self.details = details or {}
        self.setNeedsDisplay_(True)

    def _bar_layout(self):
        w = self.bounds().size.width
        return 58, w - 58 - 64  # bar_x, bar_w

    def _row_y(self, i):
        return 34 + i * 22

    @safe_draw
    def drawRect_(self, _rect):
        b = self.bounds()
        w, h = b.size.width, b.size.height
        path = A.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
            b, 12, 12)
        C(BG_CARD).set()
        path.fill()
        draw_text("模型占比（本月）", 12, 13, f_sys(9), C(FG_DIM))

        bar_x, bar_w = 58, w - 58 - 64
        row_h = 22
        y = 34
        for label, segs, total_str in self.rows:
            draw_text(label, 12, y + 4, f_sys(9), C(FG_DIM))
            total = sum(v for _m, v in segs)
            # 整条圆角裁剪，分段画竖向渐变
            track = A.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
                F.NSMakeRect(bar_x, y, bar_w, 14), 7, 7)
            if total <= 0:
                C("#24262B").set()
                track.fill()
            else:
                A.NSGraphicsContext.saveGraphicsState()
                track.addClip()
                x_cur = bar_x
                for m, v in segs:
                    sw = bar_w * (v / total)
                    c_bot, c_top = MODEL_COLORS.get(m, MODEL_COLOR_OTHER)
                    seg = A.NSBezierPath.bezierPathWithRect_(
                        F.NSMakeRect(x_cur, y, sw, 14))
                    grad = (A.NSGradient.alloc()
                            .initWithStartingColor_endingColor_(
                                C(c_bot), C(c_top)))
                    grad.drawInBezierPath_angle_(seg, 90)
                    if sw > 30:
                        draw_text_center(f"{v / total * 100:.0f}%",
                                         x_cur + sw / 2, y + 3, f_sys(8),
                                         C("#FFFFFF"))
                    x_cur += sw
                A.NSGraphicsContext.restoreGraphicsState()
            draw_text_right(total_str, w - 12, y + 3, f_num(9), C(FG))
            y += row_h

        # 图例（底部一行）
        lx = 58
        for m in self.models:
            c_bot, _ = MODEL_COLORS.get(m, MODEL_COLOR_OTHER)
            dot = A.NSBezierPath.bezierPathWithOvalInRect_(
                F.NSMakeRect(lx, y + 4, 7, 7))
            C(c_bot).set()
            dot.fill()
            name = m.replace("deepseek-v4-", "").replace("deepseek-", "")
            draw_text(name, lx + 10, y + 3, f_sys(8), C(FG_DIM))
            lx += 10 + text_w(name, f_sys(8)) + 12

        # 悬停提示：该模型的 tokens / 请求 / 金额明细
        if self.hover is not None:
            _row_i, m = self.hover
            d = self.details.get(m)
            if d:
                name = m.replace("deepseek-v4-", "").replace("deepseek-", "")
                txt = (f"{name}  {fmt_tokens(d['total_tokens'])} tok · "
                       f"{d['requests']} 次 · {fmt_cost(d['cost'])}")
                tf = f_sys(9)
                tw = text_w(txt, tf) + 16
                tx = min(max(self._hx - tw / 2, 6), w - tw - 6)
                tip = A.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
                    F.NSMakeRect(tx, 72, tw, 20), 6, 6)
                C("#3A3D41").set()
                tip.fill()
                draw_text_center(txt, tx + tw / 2, 77, tf, C(FG))

    def mouseMoved_(self, e):
        pt = self.convertPoint_fromView_(e.locationInWindow(), None)
        self._hx = pt.x
        bar_x, bar_w = self._bar_layout()
        hover = None
        for i, (_label, segs, _t) in enumerate(self.rows):
            ry = self._row_y(i)
            if ry <= pt.y <= ry + 14 and bar_x <= pt.x <= bar_x + bar_w:
                total = sum(v for _m, v in segs) or 1
                x_cur = bar_x
                for m, v in segs:
                    x_cur += bar_w * (v / total)
                    if pt.x <= x_cur:
                        hover = (i, m)
                        break
                break
        if hover != self.hover:
            self.hover = hover
            self.setNeedsDisplay_(True)

    def mouseExited_(self, _e):
        if self.hover is not None:
            self.hover = None
            self.setNeedsDisplay_(True)


class ChartCard(V):
    """近 7 天柱状图卡：标题 + 合计 + 金额/Tokens 切换 + 渐变柱。"""

    MODE_COST = "cost"
    MODE_TOKENS = "tokens"

    def initWithFrame_(self, frame):
        self = objc.super(ChartCard, self).initWithFrame_(frame)
        if self is None:
            return None
        self.last7 = []
        self.mode = self.MODE_COST
        self.hover_i = None
        ta = A.NSTrackingArea.alloc().initWithRect_options_owner_userInfo_(
            F.NSZeroRect,
            A.NSTrackingMouseEnteredAndExited | A.NSTrackingMouseMoved
            | A.NSTrackingActiveAlways | A.NSTrackingInVisibleRect,
            self, None)
        self.addTrackingArea_(ta)
        return self

    def _bar_x(self, i):
        w = self.bounds().size.width
        gap = (w - 24 - 7 * 20) / 8
        return 12 + gap + i * (20 + gap)

    def renderLast7_(self, last7):
        self.last7 = last7 or []
        self.setNeedsDisplay_(True)

    def _pill_rects(self):
        w = self.bounds().size.width
        # 右对齐两个小胶囊：金额 / Tokens
        t = F.NSMakeRect(w - 12 - 52, 30, 52, 18)
        c = F.NSMakeRect(w - 12 - 52 - 4 - 44, 30, 44, 18)
        return c, t

    @safe_draw
    def drawRect_(self, _rect):
        b = self.bounds()
        w, h = b.size.width, b.size.height
        path = A.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
            b, 12, 12)
        C(BG_CARD).set()
        path.fill()

        if self.mode == self.MODE_COST:
            title = "近 7 天消费（CNY · 估算）"
            total = fmt_cost(sum(d["cost"] for d in self.last7))
        else:
            title = "近 7 天消耗（Tokens）"
            total = fmt_tokens(sum(d["tokens"] for d in self.last7))
        draw_text(title, 12, 13, f_sys(9), C(FG_DIM))
        draw_text_right(total, w - 12, 12, f_num(11), C(FG))

        # 切换胶囊
        c_rect, t_rect = self._pill_rects()
        for rect, label, mode in (
                (c_rect, "金额", self.MODE_COST),
                (t_rect, "Tokens", self.MODE_TOKENS)):
            pp = A.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
                rect, 9, 9)
            C(TAB_ON_BG if self.mode == mode else "#24262B").set()
            pp.fill()
            draw_text_center(label, rect.origin.x + rect.size.width / 2,
                             rect.origin.y + 3.5, f_sys(8),
                             C(FG if self.mode == mode else FG_DIM))

        # 图例（左下：flash / pro 分模型颜色）
        legend_models = []
        for d in self.last7:
            for m in d.get("models", {}):
                if m not in legend_models:
                    legend_models.append(m)
        lx = 12
        for m in legend_models:
            c_bot, _c_top = MODEL_COLORS.get(m, MODEL_COLOR_OTHER)
            dot = A.NSBezierPath.bezierPathWithOvalInRect_(
                F.NSMakeRect(lx, 34, 7, 7))
            C(c_bot).set()
            dot.fill()
            name = m.replace("deepseek-v4-", "").replace("deepseek-", "")
            draw_text(name, lx + 10, 33, f_sys(8), C(FG_DIM))
            lx += 10 + text_w(name, f_sys(8)) + 12

        # 柱条（分模型堆叠：flash 在下，pro 在上）
        top, bottom = 56, h - 18
        key = "cost" if self.mode == self.MODE_COST else "tokens"
        vmax = max((d[key] for d in self.last7), default=0) or 1
        bw = 20
        gap = (w - 24 - 7 * bw) / 8
        for i, d in enumerate(self.last7):
            x0 = 12 + gap + i * (bw + gap)
            x1 = x0 + bw
            v = d[key]
            if v > 0:
                segs = [(m, d["models"][m][key])
                        for m in legend_models
                        if d.get("models", {}).get(m, {}).get(key, 0) > 0]
                y_cur = bottom
                for j, (m, sv) in enumerate(segs):
                    sh = max((bottom - top) * (sv / vmax), 2)
                    y_seg = y_cur - sh
                    c_bot, c_top = MODEL_COLORS.get(m, MODEL_COLOR_OTHER)
                    if j == len(segs) - 1:
                        # 最顶段带圆角（手工拼路径，兼容 macOS 14）
                        cr = min(4, sh / 2)
                        seg = A.NSBezierPath.bezierPath()
                        seg.moveToPoint_(F.NSMakePoint(x0, y_cur))
                        seg.lineToPoint_(F.NSMakePoint(x0, y_seg + cr))
                        seg.curveToPoint_controlPoint1_controlPoint2_(
                            F.NSMakePoint(x0 + cr, y_seg),
                            F.NSMakePoint(x0, y_seg), F.NSMakePoint(x0, y_seg))
                        seg.lineToPoint_(F.NSMakePoint(x1 - cr, y_seg))
                        seg.curveToPoint_controlPoint1_controlPoint2_(
                            F.NSMakePoint(x1, y_seg + cr),
                            F.NSMakePoint(x1, y_seg), F.NSMakePoint(x1, y_seg))
                        seg.lineToPoint_(F.NSMakePoint(x1, y_cur))
                        seg.closePath()
                    else:
                        seg = A.NSBezierPath.bezierPathWithRect_(
                            F.NSMakeRect(x0, y_seg, bw, sh))
                    grad = (A.NSGradient.alloc()
                            .initWithStartingColor_endingColor_(
                                C(c_bot), C(c_top)))
                    grad.drawInBezierPath_angle_(seg, 90)
                    y_cur = y_seg
            else:
                r = F.NSMakeRect(x0, bottom - 2, bw, 2)
                bar = A.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
                    r, 1, 1)
                C("#24262B").set()
                bar.fill()
            draw_text_center(d["day"][3:], x0 + bw / 2, h - 14,
                             f_mono(8), C(FG_DIM))

        # 悬停提示：两行气泡（合计 + 分模型）
        if (self.hover_i is not None and 0 <= self.hover_i < len(self.last7)):
            d = self.last7[self.hover_i]
            tf = f_sys(9)
            if self.mode == self.MODE_COST:
                line1 = f"{d['day']}  {fmt_cost(d['cost'])}"
                fv = lambda m: fmt_cost(d.get("models", {}).get(m, {})
                                        .get("cost", 0))
            else:
                line1 = f"{d['day']}  {fmt_tokens(d['tokens'])} tok"
                fv = lambda m: (fmt_tokens(d.get("models", {}).get(m, {})
                                           .get("tokens", 0)) + " tok")
            parts = [f"{m.replace('deepseek-v4-', '').replace('deepseek-', '')}"
                     f" {fv(m)}" for m in legend_models]
            line2 = " · ".join(parts) if parts else "—"
            tw = max(text_w(line1, tf), text_w(line2, tf)) + 16
            th = 32
            cx = self._bar_x(self.hover_i) + bw / 2
            tx = min(max(cx - tw / 2, 6), w - tw - 6)
            tip = A.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
                F.NSMakeRect(tx, top - 4, tw, th), 6, 6)
            C("#3A3D41").set()
            tip.fill()
            draw_text_center(line1, tx + tw / 2, top, tf, C(FG))
            draw_text_center(line2, tx + tw / 2, top + 13, f_sys(8), C(FG_DIM))

    def mouseMoved_(self, e):
        pt = self.convertPoint_fromView_(e.locationInWindow(), None)
        idx = None
        for i in range(len(self.last7)):
            x0 = self._bar_x(i)
            if x0 - 8 <= pt.x <= x0 + 20 + 8:
                idx = i
                break
        if idx != self.hover_i:
            self.hover_i = idx
            self.setNeedsDisplay_(True)

    def mouseExited_(self, _e):
        if self.hover_i is not None:
            self.hover_i = None
            self.setNeedsDisplay_(True)

    def mouseUp_(self, e):
        pt = self.convertPoint_fromView_(e.locationInWindow(), None)
        c_rect, t_rect = self._pill_rects()
        if A.NSPointInRect(pt, c_rect):
            self.mode = self.MODE_COST
            self.setNeedsDisplay_(True)
        elif A.NSPointInRect(pt, t_rect):
            self.mode = self.MODE_TOKENS
            self.setNeedsDisplay_(True)


# ════════════════════════════════════════════════════════════
#  服务探测逻辑（工作线程调用，subprocess only）
# ════════════════════════════════════════════════════════════

def debug(msg):
    try:
        with open(DEBUG_LOG, "a") as f:
            f.write(f"{time.strftime('%H:%M:%S')} {msg}\n")
    except Exception:
        pass


def run(cmd, timeout=10):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except Exception as e:
        return -1, str(e)


def http_ok():
    code, out = run([CURL, "-s", "-o", "/dev/null", "--max-time", "1",
                     "-w", "%{http_code}", PROBE_URL], timeout=3)
    try:
        return 200 <= int(out.strip()) <= 399
    except ValueError:
        return False


def service_pid():
    code, out = run([PGREP, "-f", "apps/cli/lib/bin.js web"])
    if code == 0 and out.strip():
        return out.strip().splitlines()[0]
    return None


def uptime_of(pid):
    code, out = run([PS, "-o", "etime=", "-p", pid])
    t = out.strip()
    return t if code == 0 and t else None


def probe_status():
    ok = http_ok()
    pid = service_pid()
    up = uptime_of(pid) if pid else None
    state = "running" if ok else ("error" if pid else "stopped")
    return state, pid, up


def load_logs():
    parts = []
    for name, path in (("stdout", "/tmp/dsh-web.out"),
                       ("stderr", "/tmp/dsh-web.err")):
        try:
            lines = open(path, errors="replace").read().splitlines()[-12:]
        except FileNotFoundError:
            lines = []
        body = "\n".join(lines) if lines else "（空）"
        parts.append(f"── {name} ──\n{body}")
    return "\n\n".join(parts)


def fmt_tokens(n):
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


def fmt_cost(c):
    return f"¥{c:.2f}" if c >= 0.01 else f"¥{c:.4f}"


# ════════════════════════════════════════════════════════════
#  主窗口 / App 委托
# ════════════════════════════════════════════════════════════

SERVICE_H = 260          # 服务页内容高度
LOGS_EXTRA = 116         # 日志展开加高
USAGE_H = 556            # 用量页内容高度


def make_label(frame, size, color, mono=False, semibold=False, center=False):
    lb = A.NSTextField.alloc().initWithFrame_(frame)
    lb.setBezeled_(False)
    lb.setDrawsBackground_(False)
    lb.setEditable_(False)
    lb.setSelectable_(False)
    lb.setFont_(f_mono(size) if mono else f_sys(size, semibold))
    lb.setTextColor_(C(color))
    if center:
        lb.setAlignment_(A.NSTextAlignmentCenter)
    return lb


class AppDelegate(A.NSObject):

    # ── 构建 ──
    def build(self):
        self.q = queue.Queue()
        self.busy = False
        self.logs_visible = False
        self.state, self.pid, self.uptime = "checking", None, None
        self.usage_data = None
        self.balance = (False, None)
        self.current_tab = "service"

        # ── 菜单栏图标 + 状态文字 ──
        self.status_item = (A.NSStatusBar.systemStatusBar()
                            .statusItemWithLength_(A.NSVariableStatusItemLength))
        btn = self.status_item.button()
        icon = (A.NSImage.imageWithSystemSymbolName_accessibilityDescription_(
            "fish.fill", "DSH")
                or A.NSImage.imageWithSystemSymbolName_accessibilityDescription_(
                    "bolt.fill", "DSH"))
        if icon:
            icon.setTemplate_(True)  # 模板图标，自动适配深浅色菜单栏
            btn.setImage_(icon)
            btn.setImagePosition_(A.NSImageLeft)
        btn.setTarget_(self)
        btn.setAction_("toggle:")
        self._update_status_item()

        # ── 弹出面板（点击图标弹出，点外部自动收起）──
        self.popover = A.NSPopover.alloc().init()
        # Transient：点面板以外任何地方就收起（和 Wi-Fi 菜单一致）
        self.popover.setBehavior_(A.NSPopoverBehaviorTransient)
        self.popover.setAnimates_(True)
        container = V.alloc().initWithFrame_(
            F.NSMakeRect(0, 0, WIN_W, SERVICE_H))
        self.container = container
        # 容器本身就是 popover 的内容视图（见下方 setView_），
        # 它的位置和尺寸由 popover 统一排布（窗口边缘有 ~13pt 内边距）。
        # 这里必须让它始终填满内容区，绝不能手动 setFrame_ 它的 origin，
        # 否则会把视图从内边距里拽到 (0,0)，造成切换选项卡时整体跳动偏左。
        container.setAutoresizingMask_(A.NSViewWidthSizable
                                       | A.NSViewHeightSizable)
        vc = A.NSViewController.alloc().init()
        vc.setView_(container)
        self.popover.setContentViewController_(vc)
        self.popover.setContentSize_(F.NSMakeSize(WIN_W, SERVICE_H))
        cv = container

        # 选项卡
        self.tab_service = PillButton.alloc().initWithFrame_(
            F.NSMakeRect(18, 12, 162, 30))
        self.tab_service.setupWithText_command_colors_fontSize_("服务状态",
                    lambda: self.switch_tab("service"), None, 11)
        cv.addSubview_(self.tab_service)
        self.tab_usage = PillButton.alloc().initWithFrame_(
            F.NSMakeRect(188, 12, 162, 30))
        self.tab_usage.setupWithText_command_colors_fontSize_("用量统计",
                    lambda: self.switch_tab("usage"), None, 11)
        cv.addSubview_(self.tab_usage)

        # ── 服务页视图 ──
        self.svc = []
        self.status_card = StatusCard.alloc().initWithFrame_(
            F.NSMakeRect(16, 52, 336, 68))
        cv.addSubview_(self.status_card)
        self.svc.append(self.status_card)

        self.busy_label = make_label(F.NSMakeRect(18, 126, 332, 14), 10,
                                     ACCENT)
        cv.addSubview_(self.busy_label)
        self.svc.append(self.busy_label)

        self.btn_start = PillButton.alloc().initWithFrame_(
            F.NSMakeRect(20, 144, 104, 34))
        self.btn_start.setupWithText_command_colors_fontSize_(
            "▶ 启动", self.on_start,
            (ACCENT, "#FFFFFF", ACCENT_HOVER, ACCENT_PRESS), 12)
        self.btn_stop = PillButton.alloc().initWithFrame_(
            F.NSMakeRect(132, 144, 104, 34))
        self.btn_stop.setupWithText_command_colors_fontSize_(
            "■ 停止", self.on_stop,
            (WHITE_BTN, "#0F1115", "#FFFFFF", "#E5E7EB"), 12)
        self.btn_refresh = PillButton.alloc().initWithFrame_(
            F.NSMakeRect(244, 144, 104, 34))
        self.btn_refresh.setupWithText_command_colors_fontSize_(
            "⟳ 刷新", self.request_refresh, None, 12)
        for b in (self.btn_start, self.btn_stop, self.btn_refresh):
            cv.addSubview_(b)
            self.svc.append(b)

        self.btn_open = PillButton.alloc().initWithFrame_(
            F.NSMakeRect(18, 184, 162, 34))
        self.btn_open.setupWithText_command_colors_fontSize_(
            "打开面板", self.on_open, None, 12)
        self.btn_logs = PillButton.alloc().initWithFrame_(
            F.NSMakeRect(188, 184, 162, 34))
        self.btn_logs.setupWithText_command_colors_fontSize_(
            "查看日志", self.on_toggle_logs, None, 12)
        for b in (self.btn_open, self.btn_logs):
            cv.addSubview_(b)
            self.svc.append(b)

        self.log_view = A.NSTextView.alloc().initWithFrame_(
            F.NSMakeRect(16, 224, 336, 100))
        self.log_view.setEditable_(False)
        self.log_view.setBackgroundColor_(C(BG_PANEL))
        self.log_view.setFont_(f_mono(9))
        self.log_view.setTextColor_(C(FG_DIM))
        self.log_view.setWantsLayer_(True)
        self.log_view.layer().setCornerRadius_(10)
        self.log_view.setHidden_(True)
        cv.addSubview_(self.log_view)
        self.svc.append(self.log_view)

        # ── 用量页视图 ──
        self.usg = []
        self.balance_card = (MetricCard.alloc()
                             .initWithFrame_title_valueSize_onClick_showPill_(
                                 F.NSMakeRect(16, 54, 200, 84),
                                 "账户余额", 20, self.on_topup, True))
        self.cost_card = (MetricCard.alloc()
                          .initWithFrame_title_valueSize_onClick_showPill_(
                              F.NSMakeRect(224, 54, 128, 84),
                              "本月消费（估算）", 20, None, False))
        self.m_tokens = (MetricCard.alloc()
                         .initWithFrame_title_valueSize_onClick_showPill_(
                             F.NSMakeRect(16, 146, 107, 72),
                             "Tokens（本月）", 14, None, False))
        self.m_req = (MetricCard.alloc()
                      .initWithFrame_title_valueSize_onClick_showPill_(
                          F.NSMakeRect(131, 146, 107, 72),
                          "API 请求次数", 14, None, False))
        self.m_hit = (MetricCard.alloc()
                      .initWithFrame_title_valueSize_onClick_showPill_(
                          F.NSMakeRect(246, 146, 106, 72),
                          "缓存命中率", 14, None, False))
        self.share_card = ModelShareCard.alloc().initWithFrame_(
            F.NSMakeRect(16, 226, 336, 96))
        self.chart = ChartCard.alloc().initWithFrame_(
            F.NSMakeRect(16, 330, 336, 140))
        self.note = make_label(F.NSMakeRect(16, 474, 336, 14), 9,
                               FG_CREDIT, center=True)
        self.btn_usage_refresh = PillButton.alloc().initWithFrame_(
            F.NSMakeRect((WIN_W - 162) / 2, 494, 162, 30))
        self.btn_usage_refresh.setupWithText_command_colors_fontSize_(
            "⟳ 刷新用量", self.request_usage,
            (ACCENT, "#FFFFFF", ACCENT_HOVER, ACCENT_PRESS), 11)
        for v in (self.balance_card, self.cost_card, self.m_tokens,
                  self.m_req, self.m_hit, self.share_card, self.chart,
                  self.note, self.btn_usage_refresh):
            cv.addSubview_(v)
            self.usg.append(v)

        # 署名 + 退出（两页共用，位置由 applyTab 按面板高度决定）
        self.credit = make_label(F.NSMakeRect(0, 0, WIN_W, 14), 9,
                                 FG_CREDIT, center=True)
        self.credit.setStringValue_("Coded by Kimi and DK")
        cv.addSubview_(self.credit)
        self.btn_quit = PillButton.alloc().initWithFrame_(
            F.NSMakeRect(WIN_W - 60, 0, 46, 18))
        self.btn_quit.setupWithText_command_colors_fontSize_(
            "退出", self.on_quit, ("#2A2C31", FG_DIM, "#3A3D41", "#24262B"), 9)
        cv.addSubview_(self.btn_quit)

        # 初始选项卡
        self.applyTab()

        # 定时器：queue 轮询 + 状态/用量自动刷新
        A.NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            0.2, self, "poll:", None, True)
        A.NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            5.0, self, "tickStatus:", None, True)
        A.NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            60.0, self, "tickUsage:", None, True)
        self.request_refresh()
        self.request_usage()
        debug("app start (menubar)")

    # ── 菜单栏图标 / 弹出层 ──
    def toggle_(self, sender):
        if self.popover.isShown():
            self.popover.performClose_(None)
        else:
            self._show_popover(sender)

    def _show_popover(self, sender):
        self.popover.showRelativeToRect_ofView_preferredEdge_(
            sender.bounds(), sender, A.NSMinYEdge)
        (A.NSApplication.sharedApplication()
         .activateIgnoringOtherApps_(True))

    def _update_status_item(self):
        """状态文字 = 状态点颜色 + 剩余总金额。"""
        dot_color = {"running": GREEN, "error": RED,
                     "stopped": GRAY, "checking": ORANGE}[self.state]
        ok, bal = self.balance
        bal_str = f"¥{bal}" if ok else "—"
        s = F.NSMutableAttributedString.alloc().initWithString_("")
        dot = F.NSAttributedString.alloc().initWithString_attributes_(
            "● ", {A.NSForegroundColorAttributeName: C(dot_color),
                   A.NSFontAttributeName: f_sys(10)})
        amt = F.NSAttributedString.alloc().initWithString_attributes_(
            bal_str, {A.NSForegroundColorAttributeName:
                      A.NSColor.labelColor(),  # 跟随系统深浅色
                      A.NSFontAttributeName: f_sys(11)})
        s.appendAttributedString_(dot)
        s.appendAttributedString_(amt)
        self.status_item.button().setAttributedTitle_(s)

    def on_quit(self):
        A.NSApplication.sharedApplication().terminate_(None)

    # ── 选项卡 ──
    def switch_tab(self, name):
        if name != self.current_tab:
            self.current_tab = name
            self.applyTab(True)

    def applyTab(self, animate=False):
        is_svc = self.current_tab == "service"
        h = SERVICE_H + (LOGS_EXTRA if (is_svc and self.logs_visible) else 0) \
            if is_svc else USAGE_H
        # 每个视图的最终可见性（log_view 只在「服务页 + 已展开」时可见）
        targets = []
        for v in self.svc:
            if v is self.log_view:
                targets.append((v, is_svc and self.logs_visible))
            else:
                targets.append((v, is_svc))
        for v in self.usg:
            targets.append((v, not is_svc))
        animate = animate and self.popover.isShown()
        if animate:
            # 交叉淡化：新内容透明入场，旧内容淡出后隐藏
            incoming = [v for v, vis in targets if vis]
            outgoing = [v for v, vis in targets
                        if not vis and not v.isHidden()]
            for v in incoming:
                v.setAlphaValue_(0.0)
                v.setHidden_(False)
            A.NSAnimationContext.beginGrouping()
            ctx = A.NSAnimationContext.currentContext()
            ctx.setDuration_(0.18)
            for v in outgoing:
                v.animator().setAlphaValue_(0.0)
            for v in incoming:
                v.animator().setAlphaValue_(1.0)

            def _fade_done():
                for v, vis in targets:
                    v.setHidden_(not vis)
                    v.setAlphaValue_(1.0)
            ctx.setCompletionHandler_(_fade_done)
            A.NSAnimationContext.endGrouping()
        else:
            for v, vis in targets:
                v.setHidden_(not vis)
        self.credit.setFrame_(F.NSMakeRect(0, h - 24, WIN_W, 18))
        self.btn_quit.setFrame_(F.NSMakeRect(WIN_W - 58, h - 24, 46, 18))
        # 高度直接切换。注意：NSPopover 的 contentSize 虽标记为可动画，
        # 但实测 animator().setContentSize_ 在主线程会挂起（popover 的
        # 窗口缩放动画与 NSAnimationContext 相互等待），此处只能瞬改；
        # 丝滑感由上方的交叉淡化承担。
        self.popover.setContentSize_(F.NSMakeSize(WIN_W, h))
        # 注意：不要手动调整 self.container 的 frame。它就是 popover 的
        # 内容视图，尺寸由 setContentSize_ 驱动、位置由 popover 排布；
        # 手动 setFrame_ 会把它拽到 (0,0)，导致面板内容跳动偏左。
        on = (TAB_ON_BG, FG, "#4A4D52", "#33363B")
        off = (BG, FG_DIM, "#24262B", "#1A1B1F")
        self.tab_service.setColors_(on if is_svc else off)
        self.tab_usage.setColors_(off if is_svc else on)
        if not is_svc and self.usage_data is None:
            self.request_usage()

    # ── 渲染（仅主线程）──
    def renderAll(self):
        self.status_card.renderState_pid_uptime_(self.state, self.pid,
                                         self.uptime)
        self.btn_start.setEnabled_(not self.busy and self.state != "running")
        self.btn_stop.setEnabled_(not self.busy and self.state != "stopped")
        self.btn_refresh.setEnabled_(not self.busy)
        self.btn_open.setEnabled_(self.state == "running")
        self._update_status_item()

    def renderUsageView(self):
        agg, (ok, bal) = self.usage_data, self.balance
        self.balance_card.setValue_unit_(f"¥{bal}" if ok else "获取失败",
                                         "CNY" if ok else None)
        m = agg["month"]
        self.cost_card.setValue_unit_(fmt_cost(m["cost"]), None)
        self.m_tokens.setValue_unit_(f"{m['total_tokens']:,}", None)
        self.m_req.setValue_unit_(str(m["requests"]), None)
        self.m_hit.setValue_unit_(f"{m['cache_hit_rate']}%", None)
        models = list(agg["by_model"].keys())
        bm = agg["by_model"]
        tok_rows = [(mdl, bm[mdl]["total_tokens"]) for mdl in models]
        cost_rows = [(mdl, bm[mdl]["cost"]) for mdl in models]
        tok_total = sum(v for _m, v in tok_rows)
        cost_total = sum(v for _m, v in cost_rows)
        details = {mdl: {"total_tokens": bm[mdl]["total_tokens"],
                         "requests": bm[mdl]["requests"],
                         "cost": bm[mdl]["cost"]} for mdl in models}
        self.share_card.setShareRows_models_details_(
            [("Tokens", tok_rows, fmt_tokens(tok_total)),
             ("金额", cost_rows, fmt_cost(cost_total))], models, details)
        self.chart.renderLast7_(agg["last7"])
        self.note.setStringValue_(
            f"本地统计（仅本机 DSH）· 金额为估算 · 更新于 {agg['generated_at']}")
        self._update_status_item()

    # ── 定时器回调 ──
    def poll_(self, _timer):
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "status":
                    self.state, self.pid, self.uptime = payload
                    self.renderAll()
                elif kind == "busy":
                    self.busy = True
                    self.busy_label.setStringValue_(payload)
                    self.renderAll()
                elif kind == "idle":
                    self.busy = False
                    self.busy_label.setStringValue_("")
                    self.request_refresh()
                elif kind == "logs":
                    self.log_view.setString_(payload)
                elif kind == "usage":
                    self.usage_data, self.balance = payload
                    self.renderUsageView()
                    self.btn_usage_refresh.setEnabled_(True)
        except queue.Empty:
            pass

    def tickStatus_(self, _timer):
        if not self.busy:
            self.request_refresh()

    def tickUsage_(self, _timer):
        self.request_usage()

    # ── 后台任务封装 ──
    def runInBackground_(self, fn):
        def wrapper():
            try:
                fn()
            except Exception:
                debug(traceback.format_exc())
        threading.Thread(target=wrapper, daemon=True).start()

    def request_refresh(self):
        if self.busy:
            return
        def work():
            self.q.put(("status", probe_status()))
            if self.logs_visible:
                self.q.put(("logs", load_logs()))
        self.runInBackground_(work)

    def request_usage(self):
        self.btn_usage_refresh.setEnabled_(False)
        self.note.setStringValue_("统计中…")
        def work():
            recs = usage_stats.collect_records()
            agg = usage_stats.aggregate(recs)
            key = usage_stats.load_api_key()
            bal = usage_stats.fetch_balance(key) if key else (False, None)
            self.q.put(("usage", (agg, bal)))
        self.runInBackground_(work)

    # ── 启动 / 停止 ──
    def on_start(self):
        self.q.put(("busy", "正在启动…"))
        def work():
            uid = os.getuid()
            code, _ = run([LAUNCHCTL, "print", f"gui/{uid}/{LABEL}"])
            if code != 0:
                run([LAUNCHCTL, "bootstrap", f"gui/{uid}", PLIST])
            else:
                run([LAUNCHCTL, "kickstart", "-k", f"gui/{uid}/{LABEL}"])
            for _ in range(20):
                if http_ok():
                    break
                time.sleep(1)
            self.q.put(("idle", None))
        self.runInBackground_(work)

    def on_stop(self):
        self.q.put(("busy", "正在停止…"))
        def work():
            run([LAUNCHCTL, "bootout", f"gui/{os.getuid()}/{LABEL}"])
            time.sleep(1)
            self.q.put(("idle", None))
        self.runInBackground_(work)

    # ── 辅助功能 ──
    def on_open(self):
        self.runInBackground_(lambda: run([OPEN, PROBE_URL]))

    def on_topup(self):
        self.runInBackground_(lambda: run([OPEN, TOPUP_URL]))

    def on_toggle_logs(self):
        self.logs_visible = not self.logs_visible
        self.btn_logs.setText_("收起日志" if self.logs_visible else "查看日志")
        self.applyTab(True)
        if self.logs_visible:
            self.runInBackground_(lambda: self.q.put(("logs", load_logs())))

    # ── 菜单栏应用永不因窗口关闭而退出 ──
    # 窗口版曾用 applicationShouldTerminateAfterLastWindowClosed_=True；
    # 菜单栏化后弹出层关闭即「最后一个窗口关闭」，会误杀整个 App，必须禁用。
    def applicationShouldTerminateAfterLastWindowClosed_(self, _app):
        return False


def main():
    app = A.NSApplication.sharedApplication()
    # 纯菜单栏应用：Accessory 模式（无 Dock 图标、无菜单栏菜单）
    app.setActivationPolicy_(A.NSApplicationActivationPolicyAccessory)

    delegate = AppDelegate.alloc().init()
    app.setDelegate_(delegate)
    delegate.build()
    app.run()
    debug("app quit")


if __name__ == "__main__":
    main()
