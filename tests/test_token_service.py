"""Token usage fallback and health semantics."""

from datetime import date
from types import SimpleNamespace
from unittest.mock import patch

from jarvis_gateway.llm.client import LLMUsage
from jarvis_gateway.services.token_service import TokenService


def test_failed_installed_opentoken_uses_local_estimate():
    with patch("jarvis_gateway.services.token_service.shutil.which", return_value=None):
        service = TokenService(opentoken_bin="/nonexistent")
    service.opentoken_bin = "/fake/installed-but-failing"
    service.add_local_tokens(100)
    snapshot = service.get_snapshot()
    assert snapshot["total"] == 100
    assert snapshot["source"] == "LocalEstimator"
    assert snapshot["health"] == "degraded"
    assert snapshot["estimated"] is True


def test_external_recovery_does_not_double_count_and_failure_falls_back(monkeypatch):
    service = TokenService()
    service.opentoken_bin = "/fake/opentoken"
    service.add_local_tokens(100)
    monkeypatch.setattr(service, "_fetch_opentoken_stats", lambda: {"TOTAL": 500, "codex": 500})
    service._poll_once()
    snapshot = service.get_snapshot()
    assert snapshot["total"] == 500
    assert snapshot["source"] == "OpenToken"
    snapshot["tools"]["TOTAL"] = -1
    assert service.get_snapshot()["total"] == 500
    monkeypatch.setattr(service, "_fetch_opentoken_stats", lambda: None)
    service._poll_once()
    assert service.get_snapshot()["total"] == 100
    assert service.get_snapshot()["health"] == "degraded"


def test_local_estimate_resets_across_days(monkeypatch):
    class FakeDate:
        current = date(2026, 10, 23)

        @classmethod
        def today(cls):
            return cls.current

    monkeypatch.setattr("jarvis_gateway.services.token_service.date", FakeDate)
    service = TokenService()
    service.add_local_tokens(100)
    FakeDate.current = date(2026, 10, 24)
    assert service.get_snapshot()["total"] == 0
    assert service.get_snapshot()["date"] == "2026-10-24"


def test_parser_rejects_truncated_or_failed_output(monkeypatch):
    service = TokenService()
    service.opentoken_bin = "/fake/opentoken"
    text = f"Daily leaderboard metric\n\nDate TOTAL codex\n---\n{date.today()} 100\n"
    monkeypatch.setattr(
        "jarvis_gateway.services.token_service.subprocess.run",
        lambda *a, **kw: SimpleNamespace(returncode=0, stdout=text),
    )
    assert service._fetch_opentoken_stats() is None
    monkeypatch.setattr(
        "jarvis_gateway.services.token_service.subprocess.run",
        lambda *a, **kw: SimpleNamespace(returncode=1, stdout=text + " 100"),
    )
    assert service._fetch_opentoken_stats() is None


def test_zero_llm_calls_do_not_create_estimated_usage():
    service = TokenService()
    service.add_llm_usage(LLMUsage())
    service.add_local_tokens(0)
    snapshot = service.get_snapshot()
    assert snapshot["total"] == 0 and snapshot["tools"] == {}
    assert snapshot["last_update"] == 0 and snapshot["llm_calls"] == 0
    assert not snapshot["estimated"] and not snapshot["incomplete"]


def test_provider_reported_tokens_keep_session_scope_and_unknown_failed_usage():
    service = TokenService()
    service.add_llm_usage(LLMUsage(calls=1, total_tokens=17))
    snapshot = service.get_snapshot()
    assert snapshot["total"] == 17 and snapshot["source"] == "GatewayUsage"
    assert not snapshot["estimated"] and snapshot["llm_calls"] == 1
    assert "非全账户账单" in service.get_stats_text()
    service.add_llm_usage(LLMUsage(calls=1, incomplete=True))
    snapshot = service.get_snapshot()
    assert snapshot["total"] == 17 and snapshot["llm_calls"] == 2
    assert snapshot["incomplete"]
    assert "用量未知" in service.get_stats_text()


def test_mixed_reported_and_estimated_tokens_are_labelled_estimated():
    service = TokenService()
    service.add_llm_usage(LLMUsage(calls=1, total_tokens=17))
    service.add_llm_usage(LLMUsage(calls=1, total_tokens=50, estimated=True))
    snapshot = service.get_snapshot()
    assert snapshot["total"] == 67 and snapshot["estimated"]
    assert snapshot["source"] == "LocalEstimator"
