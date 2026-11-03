"""语音速记：触发词判定、落盘位置、同日去重。"""

from jarvis_gateway.services import voice_notes


def test_trigger_words_strip_to_body():
    assert voice_notes.split_trigger("记一下，明早看结果") == ("明早看结果", True)
    assert voice_notes.split_trigger("帮我记下 测试与音速计") == ("测试与音速计", True)


def test_plain_sentence_is_not_a_note():
    # 句末标点会被剥掉，不能因此误判成速记请求
    assert voice_notes.split_trigger("画个用量图。") == ("画个用量图", False)
    assert voice_notes.split_trigger("今天天气怎么样")[1] is False


def test_bare_trigger_asks_for_content(monkeypatch, tmp_path):
    monkeypatch.setattr(voice_notes, "NOTE_PATH", str(tmp_path / "语音速记.md"))
    assert voice_notes.capture("记一下") == "要记什么呢？我在听。"


def test_note_lands_under_today_heading(monkeypatch, tmp_path):
    path = tmp_path / "语音速记.md"
    monkeypatch.setattr(voice_notes, "NOTE_PATH", str(path))
    reply = voice_notes.capture("记一下，明早看结果",
                                now=__import__("datetime").datetime(2026, 11, 3, 22, 43))
    assert reply == "已记下：明早看结果。"
    text = path.read_text(encoding="utf-8")
    assert "## 2026-11-03" in text
    assert "- 22:43 明早看结果" in text


def test_same_sentence_only_recorded_once(monkeypatch, tmp_path):
    import datetime

    path = tmp_path / "语音速记.md"
    monkeypatch.setattr(voice_notes, "NOTE_PATH", str(path))
    when = datetime.datetime(2026, 11, 3, 9, 0)
    assert voice_notes.capture("记录一下，买牛奶", now=when).startswith("已记下")
    assert voice_notes.capture("记录一下，买牛奶", now=when) == "这条已经记过了。"
    assert path.read_text(encoding="utf-8").count("买牛奶") == 1


def test_new_day_opens_new_section(monkeypatch, tmp_path):
    import datetime

    path = tmp_path / "语音速记.md"
    monkeypatch.setattr(voice_notes, "NOTE_PATH", str(path))
    voice_notes.capture("记一下，第一天", now=datetime.datetime(2026, 11, 3, 8, 0))
    voice_notes.capture("记一下，第二天", now=datetime.datetime(2026, 11, 4, 8, 0))
    text = path.read_text(encoding="utf-8")
    assert "## 2026-11-03" in text and "## 2026-11-04" in text
    # 新小节不会把昨天的内容搅乱
    assert text.index("第一天") < text.index("## 2026-11-04") < text.index("第二天")
