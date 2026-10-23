"""Unit tests for system action matching and safety guards."""

from jarvis_gateway.actions.system import SAFE_APP_PATTERN, SystemActionExecutor, strip_wake_word


def test_safe_app_pattern():
    assert SAFE_APP_PATTERN.match("Safari")
    assert SAFE_APP_PATTERN.match("Visual Studio Code")
    assert SAFE_APP_PATTERN.match("iTerm-2")
    # Disallow shell injection characters
    assert not SAFE_APP_PATTERN.match("Safari; rm -rf /")
    assert not SAFE_APP_PATTERN.match('Safari" && echo "hack')
    assert not SAFE_APP_PATTERN.match("App`whoami`")


def test_fast_command_matching_is_pure():
    # Planning must never execute a system action (no volume changes / screen lock in tests).
    reply, action = SystemActionExecutor.plan_fast_command("现在几点了")
    assert "星期" in reply
    assert action is None

    date_reply, _ = SystemActionExecutor.plan_fast_command("今天几号")
    assert "月" in date_reply and "日" in date_reply
    weekday_reply, _ = SystemActionExecutor.plan_fast_command("今天星期几")
    assert weekday_reply.startswith("今天是星期")
    assert "点" not in weekday_reply

    reply, action = SystemActionExecutor.plan_fast_command("放首歌听听")
    assert reply == "已开始播放音乐。"
    assert action == "music_play"

    reply, action = SystemActionExecutor.plan_fast_command("音量调到 50")
    assert action == "set_volume:50"


def test_fast_command_does_not_hijack_dictation():
    # These contain command keywords but are normal sentences and must reach the ASR/typing path.
    assert SystemActionExecutor.plan_fast_command("今天天气真不错，我们去写代码吧") is None
    assert SystemActionExecutor.plan_fast_command("现在时间不够了") is None
    assert SystemActionExecutor.plan_fast_command("我刚才把音量调到五十了") is None


def test_wake_word_is_not_part_of_the_command():
    utterance, woke = strip_wake_word("贾维斯，锁屏")
    assert woke is True
    assert utterance == "锁屏"
    reply, action = SystemActionExecutor.plan_fast_command(utterance)
    assert action == "lock_screen"
    assert reply == "已为您锁定屏幕。"

    utterance, woke = strip_wake_word("嘿，贾维斯 音量调到 30")
    assert (utterance, woke) == ("音量调到 30", True)

    utterance, woke = strip_wake_word("贾维斯")
    assert (utterance, woke) == ("", True)

    # A sentence merely containing the name must not be treated as a command.
    utterance, woke = strip_wake_word("贾维斯这个项目真不错")
    assert (utterance, woke) == ("贾维斯这个项目真不错", False)
