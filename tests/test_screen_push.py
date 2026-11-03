"""屏上图文推送：单格队列、图表落盘、给 TTS 的回复文案。"""

import pytest

from jarvis_gateway.services import screen_push


def test_queue_keeps_only_the_newest_image():
    screen_push._queue.clear()
    screen_push.push_image("http://a/1.png")
    screen_push.push_image("http://a/2.png")
    assert screen_push.pending_count() == 1
    assert screen_push.pop_image()["url"] == "http://a/2.png"
    assert screen_push.pop_image() is None


def test_blank_url_is_ignored():
    screen_push._queue.clear()
    screen_push.push_image("")
    assert screen_push.pending_count() == 0


def test_chart_url_uses_basename():
    url = screen_push.chart_url("/tmp/run/charts/tokens-1.jpg", "192.168.0.102")
    assert url == "http://192.168.0.102:18888/charts/tokens-1.jpg"


@pytest.mark.parametrize("value,text", [
    (0, "0"),
    (9999, "9999"),
    (33000000, "3300.0万"),
    (320000000, "3.20亿"),
])
def test_number_formatting(value, text):
    assert screen_push._fmt(value) == text


def test_unreadable_stats_still_pushes(monkeypatch, tmp_path):
    monkeypatch.setattr(screen_push, "CHART_DIR", str(tmp_path))
    screen_push._queue.clear()
    reply = screen_push.show_tokens("10.0.0.5", stats={"total": 0})
    assert screen_push.pending_count() == 1
    assert reply.startswith("图表已推到屏幕上了")


def test_render_writes_320x240_jpeg(monkeypatch, tmp_path):
    pytest.importorskip("PIL", reason="屏上推送的可选依赖")
    monkeypatch.setattr(screen_push, "CHART_DIR", str(tmp_path))
    stats = {
        "total": 33000000,
        "total_normalized": 2900000,
        "tools": {"dsh": 17000000, "hermes": 14000000},
    }
    path = screen_push.render_tokens(stats)
    # 板子的 image_fetch 只认 JPEG 和原始 RGB565，PNG 会被当成 RGB565 读。
    assert path.endswith(".jpg")
    from PIL import Image

    with Image.open(path) as img:
        assert img.size == (320, 240)
