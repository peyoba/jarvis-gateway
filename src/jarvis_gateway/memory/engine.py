"""
Jarvis 长期记忆引擎：对话历史、事实库与自我进化笔记。

纯标准库实现，仓库版与线上单文件网关共用同一份代码（复制即可运行）。
约定：
- 不依赖任何网络或向量库，召回用字符二元组重叠 + 重要度 + 时间衰减打分。
- 所有写入都是「临时文件 + os.replace」原子替换，文件权限 0600。
- 自我进化 = 后台定期把最近对话提炼成长期事实，并重写一小段行为笔记（persona）。
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple

DEFAULT_MAX_FACTS = 240
DEFAULT_PERSONA_CHARS = 1200
DEFAULT_HISTORY_KEEP = 400
DEFAULT_EVERY_TURNS = 6
DEFAULT_MIN_INTERVAL_SEC = 20.0

CONSOLIDATE_SYSTEM_PROMPT = "你在维护桌面语音助手贾维斯的长期记忆。只输出 JSON，不要输出解释或代码块。"

CONSOLIDATE_USER_PROMPT = """阅读最近的对话，提炼值得长期记住的信息。

值得记住：
- 用户稳定的偏好、称呼、作息、说话习惯
- 用户正在做的项目、常用工具、设备与环境
- 用户明确要求你记住的内容

不要记住：一次性指令、寒暄、临时状态，以及密码、令牌、住址等敏感信息。

已有事实（id: 内容）：
{facts}

最近对话：
{dialogue}

输出严格 JSON：
{{"facts":[{{"text":"不超过60字的事实","importance":1到5,"tags":["分类"]}}],
  "forget":[需要删除的已有事实 id],
  "persona":"不超过5条短句的自我改进笔记，以分号分隔；没有变化就填空字符串"}}

facts 只写新的或需要更正的内容，重复的不要输出。"""

_ASCII_TOKEN = re.compile(r"[a-z0-9_]+")
_HAN_RUN = re.compile(r"[\u4e00-\u9fff]+")
_PUNCT = re.compile(r"[\s，,。.、!！?？:：;；'\"“”‘’()（）\[\]【】<>《》-]+")

_REMEMBER = re.compile(r"^(?:请)?记住[：:,，\s]*(.{2,})$", re.S)
_FORGET = re.compile(r"^(?:请)?(?:忘掉|忘记|删掉记忆)[：:,，\s]*(.{1,})$", re.S)
_LIST = re.compile(r"^(?:你)?(?:都)?(?:记得|记住)(?:了)?(?:什么|哪些)[？?]?$|^记忆清单[。?？]?$|^看看(?:你的)?记忆[。?？]?$")
_CONSOLIDATE = re.compile(r"^(?:整理|刷新|重新整理)(?:一下)?(?:长期)?记忆[。!！]?$|^重新学习[。!！]?$|^整理一下你的记忆[。!！]?$")
_CLEAR = re.compile(r"^清空(?:全部)?记忆[，,。!！\s]*(确认|确定)[。!！]?$")


def _tokenize(text: str) -> Set[str]:
    """中英混排的轻量分词：英文按词，中文按单字 + 相邻二元组。"""
    lowered = (text or "").lower()
    tokens = set(_ASCII_TOKEN.findall(lowered))
    for run in _HAN_RUN.findall(lowered):
        tokens.update(run)
        tokens.update(run[i:i + 2] for i in range(len(run) - 1))
    return tokens


def _normalize(text: str) -> str:
    return _PUNCT.sub("", (text or "").lower())


def _clip(text: str, limit: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[: max(0, limit - 1)] + "…"


@dataclass
class Fact:
    id: int
    text: str
    importance: int = 3
    tags: List[str] = field(default_factory=list)
    created: float = 0.0
    last_used: float = 0.0
    hits: int = 0
    source: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id, "text": self.text, "importance": self.importance,
            "tags": list(self.tags), "created": self.created,
            "last_used": self.last_used, "hits": self.hits, "source": self.source,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Fact":
        tags = data.get("tags") or []
        return cls(
            id=int(data.get("id", 0)),
            text=str(data.get("text", "")),
            importance=max(1, min(5, int(data.get("importance", 3) or 3))),
            tags=[str(t) for t in tags][:6] if isinstance(tags, list) else [],
            created=float(data.get("created", 0) or 0),
            last_used=float(data.get("last_used", 0) or 0),
            hits=int(data.get("hits", 0) or 0),
            source=str(data.get("source", "")),
        )


class Memory:
    """对话记忆 + 长期事实 + 自我进化笔记。线程安全，可被 HTTP 线程与后台线程共用。"""

    def __init__(
        self,
        root: Optional[str] = None,
        *,
        complete: Optional[Callable[[List[Dict[str, str]]], str]] = None,
        max_facts: int = DEFAULT_MAX_FACTS,
        persona_chars: int = DEFAULT_PERSONA_CHARS,
        every_turns: int = DEFAULT_EVERY_TURNS,
        min_interval_sec: float = DEFAULT_MIN_INTERVAL_SEC,
        history_turns: int = 6,
        recall_facts: int = 5,
        enabled: bool = True,
    ):
        self.root = Path(root or os.environ.get("JARVIS_MEMORY_DIR") or "~/.jarvis/memory").expanduser()
        self.max_facts = int(max_facts)
        self.persona_chars = int(persona_chars)
        self.every_turns = max(1, int(every_turns))
        self.min_interval_sec = float(min_interval_sec)
        self.history_turns = max(1, int(history_turns))
        self.recall_facts = max(1, int(recall_facts))
        self.enabled = bool(enabled)
        self._complete = complete
        self._lock = threading.RLock()
        self._consolidating = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._facts: List[Fact] = []
        self._persona = ""
        self._meta: Dict[str, Any] = {"version": 1, "turns_total": 0, "turns_pending": 0,
                                      "consolidations": 0, "last_consolidation": 0.0}
        self._loaded = False
        self._facts_file = self.root / "facts.json"
        self._persona_file = self.root / "persona.md"
        self._meta_file = self.root / "meta.json"
        self._history_file = self.root / "history.jsonl"

    # ---------- 读写基础 ----------
    def _ensure_root(self) -> None:
        if not self.root.exists():
            self.root.mkdir(parents=True, exist_ok=True)
            try:
                os.chmod(str(self.root), 0o700)
            except OSError:
                pass

    def _write_text(self, path: Path, text: str) -> None:
        self._ensure_root()
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        try:
            os.chmod(str(tmp), 0o600)
        except OSError:
            pass
        os.replace(str(tmp), str(path))

    def _write_json(self, path: Path, payload: Any) -> None:
        self._write_text(path, json.dumps(payload, ensure_ascii=False, indent=1))

    def _read_json(self, path: Path, default: Any) -> Any:
        try:
            with path.open("r", encoding="utf-8") as handle:
                return json.load(handle)
        except (OSError, ValueError):
            return default

    def load(self) -> "Memory":
        with self._lock:
            if self._loaded:
                return self
            raw = self._read_json(self._facts_file, {"facts": []})
            facts = []
            for item in raw.get("facts", []) if isinstance(raw, dict) else []:
                if isinstance(item, dict) and str(item.get("text", "")).strip():
                    facts.append(Fact.from_dict(item))
            self._facts = facts
            meta = self._read_json(self._meta_file, {})
            if isinstance(meta, dict):
                self._meta.update(meta)
            try:
                self._persona = self._persona_file.read_text(encoding="utf-8").strip()
            except OSError:
                self._persona = ""
            self._loaded = True
            return self

    def _save_facts(self) -> None:
        self._write_json(self._facts_file, {"version": 1, "facts": [f.as_dict() for f in self._facts]})

    def _save_meta(self) -> None:
        self._write_json(self._meta_file, self._meta)

    # ---------- 对话历史 ----------
    def record_exchange(self, heard: str, reply: str, mode: str = "dialog",
                        actions: Sequence[str] = ()) -> None:
        if not self.enabled or not (heard or "").strip():
            return
        entry = {"ts": time.time(), "heard": _clip(heard, 500), "reply": _clip(reply, 800),
                 "mode": mode, "actions": list(actions)[:5]}
        self._ensure_root()
        with self._lock:
            try:
                with self._history_file.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
                try:
                    os.chmod(str(self._history_file), 0o600)
                except OSError:
                    pass
                self._trim_history()
            except OSError:
                pass
            self._meta["turns_total"] = int(self._meta.get("turns_total", 0)) + 1
            self._meta["turns_pending"] = int(self._meta.get("turns_pending", 0)) + 1
            self._save_meta()

    def _trim_history(self) -> None:
        try:
            if self._history_file.stat().st_size < 512 * 1024:
                return
            lines = self._history_file.read_text(encoding="utf-8", errors="replace").splitlines()
            if len(lines) > DEFAULT_HISTORY_KEEP:
                self._write_text(self._history_file, "\n".join(lines[-DEFAULT_HISTORY_KEEP:]) + "\n")
        except OSError:
            pass

    def recent_exchanges(self, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        limit = limit or self.history_turns
        try:
            with self._history_file.open("r", encoding="utf-8", errors="replace") as handle:
                lines = handle.readlines()[-max(1, limit):]
        except OSError:
            return []
        out = []
        for line in lines:
            try:
                item = json.loads(line)
            except ValueError:
                continue
            if isinstance(item, dict) and item.get("heard"):
                out.append(item)
        return out

    def chat_history(self, turns: Optional[int] = None) -> List[Dict[str, str]]:
        """最近若干轮对话，转成 chat messages（不含本轮）。"""
        messages: List[Dict[str, str]] = []
        for item in self.recent_exchanges(turns or self.history_turns):
            heard = str(item.get("heard") or "").strip()
            reply = str(item.get("reply") or "").strip()
            if heard:
                messages.append({"role": "user", "content": _clip(heard, 200)})
            if reply:
                messages.append({"role": "assistant", "content": _clip(reply, 200)})
        return messages

    # ---------- 事实库 ----------
    def add_fact(self, text: str, importance: int = 3, tags: Optional[Sequence[str]] = None,
                 source: str = "") -> Tuple[Fact, bool]:
        text = _clip(text, 120)
        with self._lock:
            self.load()
            if not text:
                raise ValueError("empty fact")
            norm = _normalize(text)
            for fact in self._facts:
                if _normalize(fact.text) == norm:
                    fact.importance = max(fact.importance, max(1, min(5, importance)))
                    fact.last_used = time.time()
                    self._save_facts()
                    return fact, False
            fact = Fact(
                id=max([f.id for f in self._facts], default=0) + 1,
                text=text,
                importance=max(1, min(5, int(importance))),
                tags=[str(t) for t in (tags or [])][:5],
                created=time.time(), source=source,
            )
            self._facts.append(fact)
            if len(self._facts) > self.max_facts:
                self._evict()
            self._save_facts()
            return fact, True

    def _evict(self) -> None:
        now = time.time()

        def rank(fact: Fact) -> float:
            days = max(0.0, now - (fact.last_used or fact.created)) / 86400.0
            return fact.importance * 2 + fact.hits + 3.0 / (1.0 + days)

        self._facts.sort(key=rank)
        del self._facts[: max(0, len(self._facts) - self.max_facts)]

    def forget(self, query: str) -> List[str]:
        target = _normalize(query)
        if not target:
            return []
        with self._lock:
            self.load()
            removed: List[str] = []
            keep: List[Fact] = []
            target_tokens = _tokenize(query)
            for fact in self._facts:
                norm = _normalize(fact.text)
                overlap = len(target_tokens & _tokenize(fact.text))
                if target in norm or norm in target or (target_tokens and overlap / len(target_tokens) >= 0.6):
                    removed.append(fact.text)
                else:
                    keep.append(fact)
            if removed:
                self._facts = keep
                self._save_facts()
            return removed

    def facts(self) -> List[Fact]:
        with self._lock:
            self.load()
            return list(self._facts)

    def recall(self, query: str, limit: Optional[int] = None) -> List[Fact]:
        limit = limit or self.recall_facts
        query_tokens = _tokenize(query)
        if not query_tokens:
            return []
        # 单字（尤其是「的」「了」）几乎不能表达主题，只做退路：优先用二元组/英文词判定命中。
        query_key = {token for token in query_tokens if len(token) > 1} or query_tokens
        now = time.time()
        scored: List[Tuple[float, Fact]] = []
        with self._lock:
            self.load()
            for fact in self._facts:
                fact_tokens = _tokenize(fact.text)
                fact_key = {token for token in fact_tokens if len(token) > 1} or fact_tokens
                if not fact_key:
                    continue
                overlap = len(query_key & fact_key)
                if not overlap:
                    continue
                weight = overlap / ((len(query_key) * len(fact_key)) ** 0.5)
                days = max(0.0, now - (fact.last_used or fact.created)) / 86400.0
                score = 0.62 * weight + 0.23 * (fact.importance / 5.0) + 0.15 / (1.0 + days / 30.0)
                scored.append((score, fact))
        scored.sort(key=lambda pair: (pair[0], pair[1].importance), reverse=True)
        return [fact for _, fact in scored[:limit]]

    def touch(self, facts: Iterable[Fact]) -> None:
        ids = {fact.id for fact in facts}
        if not ids:
            return
        with self._lock:
            now = time.time()
            changed = False
            for fact in self._facts:
                if fact.id in ids:
                    fact.hits += 1
                    fact.last_used = now
                    changed = True
            if changed:
                self._save_facts()

    # ---------- 自我进化笔记 ----------
    def persona(self) -> str:
        with self._lock:
            self.load()
            return self._persona

    def set_persona(self, text: str) -> None:
        text = _clip(text, self.persona_chars)
        with self._lock:
            self.load()
            if text == self._persona:
                return
            self._persona = text
            self._write_text(self._persona_file, text + ("\n" if text else ""))

    def context_block(self, query: str, limit: Optional[int] = None) -> str:
        """拼给系统提示词的长期记忆片段；没有内容时返回空串。"""
        if not self.enabled:
            return ""
        facts = self.recall(query, limit)
        self.touch(facts)
        persona = self.persona()
        if not facts and not persona:
            return ""
        parts: List[str] = []
        if facts:
            lines = ["[长期记忆]（仅供参考，不要逐条复述）"]
            for index, fact in enumerate(facts, 1):
                lines.append(str(index) + ". " + fact.text)
            parts.append("\n".join(lines))
        if persona:
            parts.append("[自我进化笔记]\n" + persona)
        return "\n\n".join(parts)

    # ---------- 自然语言指令 ----------
    def handle_command(self, text: str) -> Optional[Tuple[str, bool]]:
        """本地即时处理的记忆指令，返回 (回复, 是否触发整理)；不命中返回 None。"""
        if not self.enabled:
            return None
        raw = (text or "").strip()
        if not raw:
            return None
        match = _REMEMBER.match(raw)
        if match:
            content = match.group(1).strip()
            if len(content) < 2:
                return None
            fact, created = self.add_fact(content, importance=5, source="user")
            head = "记下了" if created else "这条已经记住了"
            return head + "：" + _clip(fact.text, 20), False
        match = _FORGET.match(raw)
        if match:
            removed = self.forget(match.group(1).strip())
            if not removed:
                return "没找到这条记忆。", False
            return "已忘掉：" + _clip(removed[0], 20), False
        if _LIST.match(raw):
            facts = sorted(self.facts(), key=lambda f: (f.importance, f.last_used), reverse=True)
            if not facts:
                return "我还没记住什么，你可以说「记住：……」。", False
            top = "；".join(_clip(f.text, 18) for f in facts[:4])
            return "我记住了" + str(len(facts)) + "件事：" + top, False
        if _CONSOLIDATE.match(raw):
            return "正在整理长期记忆，稍后生效。", True
        if _CLEAR.match(raw):
            self.clear()
            return "长期记忆已清空。", False
        return None

    def wants_consolidation(self, text: str) -> bool:
        return bool(_CONSOLIDATE.match((text or "").strip()))

    # ---------- 整理（自我进化） ----------
    def _build_consolidation_messages(self) -> List[Dict[str, str]]:
        facts = self.facts()
        fact_lines = "\n".join(str(f.id) + ": " + f.text for f in facts[-40:]) or "（空）"
        dialogue = []
        for item in self.recent_exchanges(max(12, self.every_turns * 2)):
            dialogue.append("用户：" + _clip(str(item.get("heard", "")), 160))
            dialogue.append("贾维斯：" + _clip(str(item.get("reply", "")), 160))
        user = CONSOLIDATE_USER_PROMPT.format(facts=fact_lines, dialogue="\n".join(dialogue))
        return [
            {"role": "system", "content": CONSOLIDATE_SYSTEM_PROMPT},
            {"role": "user", "content": user},
        ]

    def due(self) -> bool:
        with self._lock:
            pending = int(self._meta.get("turns_pending", 0))
            last = float(self._meta.get("last_consolidation", 0) or 0)
        return pending >= self.every_turns and (time.time() - last) >= self.min_interval_sec

    def note_turn(self) -> None:
        """一轮对话结束后调用；到点就在后台整理，不阻塞回复。"""
        if not self.enabled or self._complete is None:
            return
        if not self.due():
            return
        if not self._consolidating.acquire(blocking=False):
            return
        self._consolidating.release()
        self._thread = threading.Thread(target=self._consolidate_worker, daemon=True, name="JarvisMemory")
        self._thread.start()

    def _consolidate_worker(self) -> None:
        try:
            self.consolidate()
        except Exception as exc:  # 后台线程不能把异常抛给解释器
            print("[memory] 整理失败: " + type(exc).__name__, flush=True)

    def consolidate(self, force: bool = False) -> bool:
        """同步执行一次整理；成功返回 True。调用方可以选择放在后台线程。"""
        if not self.enabled or self._complete is None:
            return False
        if not force and not self.due():
            return False
        if not self._consolidating.acquire(blocking=False):
            return False
        try:
            messages = self._build_consolidation_messages()
            try:
                raw = self._complete(messages) or ""
            except Exception as exc:
                print("[memory] 模型调用失败: " + type(exc).__name__, flush=True)
                return False
            parsed = parse_consolidation(raw)
            if not parsed:
                # 模型明确回了合法 JSON 但没有新内容：也算走过一轮，
                # 否则待整理计数会一直挂着，每隔 20 秒就去打扰模型。
                if _extract_json(raw) is not None:
                    with self._lock:
                        self._meta["turns_pending"] = 0
                        self._meta["last_consolidation"] = time.time()
                        self._save_meta()
                return False
            self.apply_consolidation(parsed)
            return True
        finally:
            self._consolidating.release()

    def apply_consolidation(self, parsed: Dict[str, Any]) -> None:
        with self._lock:
            self.load()
            forget_ids = {int(i) for i in parsed.get("forget", [])
                          if isinstance(i, (int, float)) and not isinstance(i, bool)}
            if forget_ids:
                self._facts = [f for f in self._facts if f.id not in forget_ids]
            for item in parsed.get("facts", []):
                text = str(item.get("text", "")).strip()
                if not text:
                    continue
                importance = item.get("importance", 3)
                try:
                    importance = int(importance)
                except (TypeError, ValueError):
                    importance = 3
                tags = item.get("tags", [])
                self._add_fact_locked(text, importance, tags if isinstance(tags, list) else [])
            self._save_facts()
            persona = parsed.get("persona")
            if isinstance(persona, str) and persona.strip():
                text = _clip(persona.strip(), self.persona_chars)
                if text != self._persona:
                    self._persona = text
                    self._write_text(self._persona_file, text + "\n")
            self._meta["consolidations"] = int(self._meta.get("consolidations", 0)) + 1
            self._meta["turns_pending"] = 0
            self._meta["last_consolidation"] = time.time()
            self._save_meta()

    def _add_fact_locked(self, text: str, importance: int, tags: Sequence[str]) -> None:
        text = _clip(text, 120)
        norm = _normalize(text)
        for fact in self._facts:
            if _normalize(fact.text) == norm:
                fact.importance = max(fact.importance, max(1, min(5, importance)))
                return
        self._facts.append(Fact(
            id=max([f.id for f in self._facts], default=0) + 1,
            text=text, importance=max(1, min(5, importance)),
            tags=[str(t) for t in tags][:5], created=time.time(), source="consolidation",
        ))
        if len(self._facts) > self.max_facts:
            self._evict()

    # ---------- 观测 ----------
    def summary(self) -> Dict[str, Any]:
        with self._lock:
            self.load()
            facts = len(self._facts)
            persona_chars = len(self._persona)
            meta = dict(self._meta)
            preview = [
                fact.text for fact in sorted(
                    self._facts, key=lambda f: (f.importance, f.created), reverse=True,
                )[:3]
            ]
        return {
            "enabled": self.enabled,
            "facts": facts,
            "recent_facts": preview,
            "persona_chars": persona_chars,
            "turns_total": int(meta.get("turns_total", 0)),
            "turns_pending": int(meta.get("turns_pending", 0)),
            "consolidations": int(meta.get("consolidations", 0)),
            "last_consolidation": float(meta.get("last_consolidation", 0) or 0),
            "dir": str(self.root),
        }

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            self.load()
            return {
                "summary": self.summary(),
                "persona": self._persona,
                "facts": [f.as_dict() for f in sorted(
                    self._facts, key=lambda f: (f.importance, f.last_used), reverse=True)],
                "recent": self.recent_exchanges(8),
            }

    def clear(self) -> None:
        with self._lock:
            self._facts = []
            self._persona = ""
            self._save_facts()
            self._write_text(self._persona_file, "")
            self._meta.update({"turns_pending": 0, "consolidations": 0, "last_consolidation": 0.0})
            self._save_meta()
            try:
                self._history_file.unlink()
            except OSError:
                pass


def _extract_json(raw: str) -> Optional[Dict[str, Any]]:
    text = (raw or "").strip()
    if not text:
        return None
    fence = chr(96) * 3
    text = re.sub("^" + fence + r"(?:json)?|" + fence + "$", "", text, flags=re.M).strip()
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(text[start:end + 1])
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def parse_consolidation(raw: str) -> Optional[Dict[str, Any]]:
    """把模型输出解析成 {facts, forget, persona}；结构不合法就整体丢弃。"""
    data = _extract_json(raw)
    if data is None:
        return None
    facts_out: List[Dict[str, Any]] = []
    facts = data.get("facts", [])
    if isinstance(facts, list):
        for item in facts:
            if isinstance(item, dict):
                text = str(item.get("text", "")).strip()
            elif isinstance(item, str):
                text = item.strip()
            else:
                continue
            if not text or len(text) > 160:
                continue
            try:
                importance = int(item.get("importance", 3)) if isinstance(item, dict) else 3
            except (TypeError, ValueError):
                importance = 3
            tags = item.get("tags", []) if isinstance(item, dict) else []
            facts_out.append({
                "text": text,
                "importance": max(1, min(5, importance)),
                "tags": [str(t)[:12] for t in tags][:5] if isinstance(tags, list) else [],
            })
    forget: List[int] = []
    raw_forget = data.get("forget", [])
    if isinstance(raw_forget, list):
        for value in raw_forget:
            if isinstance(value, bool):
                continue
            if isinstance(value, (int, float)):
                forget.append(int(value))
            elif isinstance(value, str) and value.strip().isdigit():
                forget.append(int(value.strip()))
    persona = data.get("persona")
    persona = persona.strip() if isinstance(persona, str) else ""
    if not facts_out and not forget and not persona:
        return None
    return {"facts": facts_out, "forget": forget, "persona": persona}
