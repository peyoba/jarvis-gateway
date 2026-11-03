#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""语音速记：把板子上一句话落到 Obsidian 收件箱。

由网关 fast_mac_command 判断触发词后调用，独立成模块便于单测。
设计约定：
- 只追加，不改动既有内容；任何异常都返回友好提示，不抛给对话主流程；
- 当天第一次记录时自动建 "## YYYY-MM-DD" 小节；
- 同一天重复说同一句只记一次（TTS 重试/重复确认不会写重）。
"""
import datetime
import os
import re
import threading

VAULT_DIR = os.path.expanduser("~/Documents/Obsidian Vault")
NOTE_PATH = os.environ.get("JARVIS_VOICE_NOTE") or os.path.join(
    VAULT_DIR, "90-收件箱", "语音速记.md")
NOTE_TITLE = "# 语音速记"

TRIGGERS = (
    "帮我记一下", "帮我记一笔", "帮我记下", "帮我记录", "帮我记",
    "记录一下", "记一下", "记下来", "记一笔", "记个事", "记到",
    "速记", "记下",
)
# 长词优先，"帮我记下" 必须先于 "帮我记" 匹配，否则会吃掉 "下" 字
_TRIGGERS_BY_LEN = tuple(sorted(TRIGGERS, key=len, reverse=True))
_LEAD = re.compile(r"^[\s，,。.、：:；;—-]+")
_lock = threading.Lock()


def split_trigger(text):
    """剥掉触发词和多余标点，同时告诉调用方到底有没有触发词。

    只看 "剥完的字符串和原文不一样" 是不够的：句末的 "。" 也会被剥掉，
    于是 "画个用量图。" 会被误判成速记请求。所以单独返回 matched。
    """
    rest = (text or "").strip()
    matched = False
    while True:
        for trig in _TRIGGERS_BY_LEN:
            if rest.startswith(trig):
                rest = rest[len(trig):]
                rest = _LEAD.sub("", rest)
                matched = True
                break
        else:
            break
    return rest.strip(" ，,。.、；;"), matched


def strip_trigger(text):
    """只要内容，不要 matched。"""
    return split_trigger(text)[0]


def _insert_into(text, date_str, line):
    """把 line 插到当天小节末尾；没有当天小节就在文末新建。"""
    marker = "## " + date_str
    lines = text.splitlines()
    if text and not text.endswith("\n"):
        lines.append("")
    try:
        start = lines.index(marker)
    except ValueError:
        out = list(lines)
        if out and out[-1].strip():
            out.append("")
        out.extend([marker, line])
        return "\n".join(out).rstrip("\n") + "\n"

    end = len(lines)
    for i in range(start + 1, len(lines)):
        if lines[i].startswith("## "):
            end = i
            break
    body = [l for l in lines[start + 1:end]]
    while body and not body[-1].strip():
        body.pop()
    body.append(line)
    out = lines[:start + 1] + body + lines[end:]
    return "\n".join(out).rstrip("\n") + "\n"


def _section_lines(lines, date_str):
    """当天小节的正文行（不含标题），没有该小节返回 None。"""
    marker = "## " + date_str
    if marker not in lines:
        return None
    start = lines.index(marker)
    end = len(lines)
    for i in range(start + 1, len(lines)):
        if lines[i].startswith("## "):
            end = i
            break
    return lines[start + 1:end]


def capture(text, now=None):
    """听到一句话就试着记下来；不是速记请求返回 None。"""
    body, triggered = split_trigger(text)
    if not triggered:
        return None
    if not body:
        return "要记什么呢？我在听。"

    now = now or datetime.datetime.now()
    date_str = now.strftime("%Y-%m-%d")
    line = "- %s %s" % (now.strftime("%H:%M"), body)

    with _lock:
        try:
            os.makedirs(os.path.dirname(NOTE_PATH), exist_ok=True)
            if os.path.exists(NOTE_PATH):
                with open(NOTE_PATH, "r", encoding="utf-8") as f:
                    text_now = f.read()
            else:
                text_now = NOTE_TITLE + "\n"
            today = _section_lines(text_now.splitlines(), date_str) or []
            if any(l.split(" ", 2)[-1].strip() == body for l in today if l.strip()):
                return "这条已经记过了。"
            updated = _insert_into(text_now, date_str, line)
            with open(NOTE_PATH, "w", encoding="utf-8") as f:
                f.write(updated)
        except Exception as e:
            print("[!] 速记写入失败: %s" % e, flush=True)
            return "速记没写进去，我记在本机日志里了。"
    print("[*] 语音速记: %s" % line, flush=True)
    return "已记下：" + body + "。"


if __name__ == "__main__":
    import sys
    print(capture(" ".join(sys.argv[1:]) or "记一下，测试一条"))
