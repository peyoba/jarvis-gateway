#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成 Jarvis.app 图标（需要 Pillow）：
    python3 make_icon.py
输出 build/AppIcon.icns 与 build/icon_1024.png
"""

import os
import subprocess
from PIL import Image, ImageDraw, ImageFilter

HERE = os.path.dirname(os.path.abspath(__file__))
S = 1024

# ---------- 背景：深海军蓝渐变 + 圆角 ----------
bg = Image.new("RGBA", (S, S))
d = ImageDraw.Draw(bg)
top = (9, 14, 26)
bottom = (17, 28, 49)
for y in range(S):
    t = y / (S - 1)
    color = tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3))
    d.line([(0, y), (S, y)], fill=color + (255,))

mask = Image.new("L", (S, S), 0)
ImageDraw.Draw(mask).rounded_rectangle([64, 64, S - 64, S - 64], radius=220, fill=255)

icon = Image.new("RGBA", (S, S), (0, 0, 0, 0))
icon.paste(bg, (0, 0), mask)

# ---------- 外圈光晕 ----------
ring = Image.new("RGBA", (S, S), (0, 0, 0, 0))
rd = ImageDraw.Draw(ring)
rd.ellipse([S / 2 - 330, S / 2 - 330, S / 2 + 330, S / 2 + 330],
           outline=(96, 165, 250, 90), width=10)
ring = ring.filter(ImageFilter.GaussianBlur(20))
icon.alpha_composite(ring)

# 细边框
ImageDraw.Draw(icon).rounded_rectangle(
    [64, 64, S - 64, S - 64], radius=220, outline=(96, 165, 250, 55), width=5)

# ---------- 声波柱 ----------
bars = [
    (0.26, (52, 211, 153)),
    (0.46, (45, 212, 191)),
    (0.68, (56, 189, 248)),
    (0.88, (96, 165, 250)),
    (0.68, (56, 189, 248)),
    (0.46, (45, 212, 191)),
    (0.26, (52, 211, 153)),
]
bar_w = 54
gap = 36
total = len(bars) * bar_w + (len(bars) - 1) * gap
x = (S - total) / 2
max_h = 560

wave = Image.new("RGBA", (S, S), (0, 0, 0, 0))
wd = ImageDraw.Draw(wave)
for factor, color in bars:
    h = max_h * factor
    y0 = (S - h) / 2
    wd.rounded_rectangle([x, y0, x + bar_w, y0 + h], radius=bar_w / 2, fill=color + (255,))
    x += bar_w + gap

# 发光层
glow = wave.filter(ImageFilter.GaussianBlur(26))
icon.alpha_composite(Image.new("RGBA", (S, S), (0, 0, 0, 0)))
icon.alpha_composite(glow)
icon.alpha_composite(wave)

# 顶部高光
shine = Image.new("RGBA", (S, S), (0, 0, 0, 0))
ImageDraw.Draw(shine).ellipse([150, 80, S - 150, 520], fill=(255, 255, 255, 12))
shine = shine.filter(ImageFilter.GaussianBlur(60))
icon.alpha_composite(Image.composite(shine, Image.new("RGBA", (S, S), (0, 0, 0, 0)), mask))

icon.save(os.path.join(HERE, "icon_1024.png"))

# ---------- iconset ----------
iconset = os.path.join(HERE, "Jarvis.iconset")
os.makedirs(iconset, exist_ok=True)
specs = [
    ("icon_16x16.png", 16), ("icon_16x16@2x.png", 32),
    ("icon_32x32.png", 32), ("icon_32x32@2x.png", 64),
    ("icon_128x128.png", 128), ("icon_128x128@2x.png", 256),
    ("icon_256x256.png", 256), ("icon_256x256@2x.png", 512),
    ("icon_512x512.png", 512), ("icon_512x512@2x.png", 1024),
]
for name, size in specs:
    icon.resize((size, size), Image.LANCZOS).save(os.path.join(iconset, name))

subprocess.run(["iconutil", "-c", "icns", iconset, "-o", os.path.join(HERE, "AppIcon.icns")],
               check=True)
print("icon ok:", os.path.join(HERE, "AppIcon.icns"))
