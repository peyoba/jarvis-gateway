#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""屏上图文推送：网关把数据画成 JPEG，板子轮询 /screen 取走并显示。

用 JPEG 而不是 PNG：板子的 image_fetch 只认 JPEG 和原始 RGB565（tjpgd 解码），
PNG 会被当成 RGB565 读，报 "raw data must be whole rows of RGB565"。

板子侧（main/screen_push.c）每 3 秒 GET 一次 /screen，拿到 url 就用现成的
image_fetch 下载并绘制到屏幕中间。队列只有一格，后来的图覆盖前一张，避免板子
离线时堆一堆过期图。

Pillow 只在真的要画图时才导入：不装 Pillow 的机器照样能 import 本模块、跑测试。
"""
import datetime
import json
import os
import threading
import urllib.request
from collections import deque

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CHART_DIR = os.environ.get("JARVIS_CHART_DIR") or os.path.join(BASE_DIR, "run", "charts")
TOKEN_STATS_URL = os.environ.get("JARVIS_TOKEN_STATS_URL") or "http://127.0.0.1:8080/token-stats"
CHART_TTL_SEC = 600
CHART_KEEP = 20

W, H = 320, 240
BG = (245, 238, 230)
INK = (58, 48, 40)
MUTED = (140, 126, 112)
ACCENT = (198, 124, 78)
BAR = (222, 196, 168)
RULE = (224, 210, 194)

_FONT_CANDIDATES = (
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "/Library/Fonts/Arial Unicode.ttf",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
)
_fonts = {}
_queue = deque(maxlen=1)
_lock = threading.Lock()


def _pil():
    """延迟导入 Pillow，缺库时给出可读的提示。"""
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError as exc:
        raise RuntimeError("屏上推送需要 Pillow：python -m pip install pillow") from exc
    return Image, ImageDraw, ImageFont


def _font(size):
    if size in _fonts:
        return _fonts[size]
    _Image, _ImageDraw, ImageFont = _pil()
    font = None
    for path in _FONT_CANDIDATES:
        if os.path.exists(path):
            try:
                font = ImageFont.truetype(path, size)
                break
            except Exception:
                continue
    if font is None:
        font = ImageFont.load_default()
    _fonts[size] = font
    return font


def _fmt(v):
    try:
        v = int(v)
    except Exception:
        return "--"
    if v >= 100000000:
        return "%.2f亿" % (v / 100000000.0)
    if v >= 10000:
        return "%.1f万" % (v / 10000.0)
    return str(v)


def fetch_token_stats(timeout=3):
    """读本机用量服务；读不到就返回空字典，让调用方决定怎么提示。"""
    try:
        req = urllib.request.urlopen(TOKEN_STATS_URL, timeout=timeout)
        return json.loads(req.read().decode("utf-8"))
    except Exception:
        return {}


def render_tokens(stats=None, now=None):
    """把今日用量画成 320x240 的 JPEG，返回本地路径。"""
    Image, ImageDraw, _ImageFont = _pil()
    stats = stats or fetch_token_stats()
    now = now or datetime.datetime.now()
    total = stats.get("total", 0) or 0
    normalized = stats.get("total_normalized", 0) or 0
    tools = stats.get("tools") or {}
    dsh = tools.get("dsh", 0) or 0
    hermes = tools.get("hermes", 0) or 0

    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    d.text((14, 10), "今日 Token", font=_font(15), fill=MUTED)
    d.text((14, 30), _fmt(total), font=_font(34), fill=INK)
    d.text((14, 70), "含缓存口径", font=_font(13), fill=MUTED)
    d.line((14, 90, W - 14, 90), fill=RULE)

    rows = [
        ("含缓存", total),
        ("不含缓存", normalized),
        ("Dsh", dsh),
        ("Hermes", hermes),
    ]
    top = 102
    row_h = 27
    bar_x = 88
    bar_w = 138
    peak = max([v for _, v in rows] + [1])
    for i, (label, value) in enumerate(rows):
        y = top + i * row_h
        d.text((14, y), label, font=_font(14), fill=INK)
        filled = int(bar_w * (float(value) / peak)) if value else 0
        d.rounded_rectangle((bar_x, y + 2, bar_x + bar_w, y + 14), radius=6, fill=BAR)
        if filled > 4:
            d.rounded_rectangle((bar_x, y + 2, bar_x + filled, y + 14), radius=6, fill=ACCENT)
        d.text((W - 14, y), _fmt(value), font=_font(13), fill=MUTED, anchor="ra")

    d.line((14, H - 30, W - 14, H - 30), fill=RULE)
    d.text((14, H - 23), "贾维斯 · %s" % now.strftime("%m-%d %H:%M"), font=_font(12), fill=MUTED)

    os.makedirs(CHART_DIR, exist_ok=True)
    name = "tokens-%s.jpg" % now.strftime("%Y%m%d-%H%M%S")
    path = os.path.join(CHART_DIR, name)
    # 基线 JPEG（不用 progressive）：板子里的 tjpgd 只认基线。
    img.save(path, "JPEG", quality=88, optimize=True, progressive=False)
    _sweep(now)
    return path


def _sweep(now=None):
    """删掉过期的图，最多留 CHART_KEEP 张。"""
    now = now or datetime.datetime.now()
    try:
        files = sorted(os.listdir(CHART_DIR))
    except Exception:
        return
    keep = set(files[-CHART_KEEP:])
    for name in files:
        if name in keep:
            continue
        path = os.path.join(CHART_DIR, name)
        try:
            age = now.timestamp() - os.path.getmtime(path)
        except Exception:
            continue
        if age > CHART_TTL_SEC:
            try:
                os.remove(path)
            except Exception:
                pass


def push_image(url, row=-1):
    """把一张图放进只有一格的待推队列（后来的覆盖先前的）。"""
    if not url:
        return
    with _lock:
        _queue.append({"url": url, "row": row,
                       "at": datetime.datetime.now().isoformat(timespec="seconds")})


def pop_image():
    """板子来取图；没有就返回 None。"""
    with _lock:
        if not _queue:
            return None
        return _queue.popleft()


def pending_count():
    with _lock:
        return len(_queue)


def chart_url(path, lan_ip, port=18888):
    return "http://%s:%d/charts/%s" % (lan_ip, port, os.path.basename(path))


def show_tokens(lan_ip, port=18888, stats=None):
    """画今日用量并推到板子，返回给 TTS 的一句话。"""
    stats = stats or fetch_token_stats()
    path = render_tokens(stats)
    push_image(chart_url(path, lan_ip, port))
    if not stats:
        return "图表已推到屏幕上了，但没读到用量数据。"
    return "图表已推到屏幕上了。含缓存 %s，不含缓存 %s。" % (
        stats.get("total_str") or _fmt(stats.get("total", 0)),
        stats.get("total_normalized_str") or _fmt(stats.get("total_normalized", 0)),
    )
