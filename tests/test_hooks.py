import json
import time

import pytest

import autoresume_plugin as plugin
from autoresume_plugin.limits import Window


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Isolated state dir, no network, cron calls captured instead of executed."""
    scheduled = []
    monkeypatch.setattr(plugin, "_STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(plugin.limits, "_CACHE", {})
    plugin._PENDING.clear()

    def fake_schedule(*, session_id, resume_at, provider="anthropic"):
        scheduled.append((session_id, resume_at, provider))
        return f"job-{len(scheduled)}"

    monkeypatch.setattr(plugin, "schedule_resume_job", fake_schedule)
    exhausted = [Window("Session", 100.0, time.time() + 3 * 3600)]
    monkeypatch.setattr(plugin.limits, "fetch_windows_cached", lambda provider: exhausted)
    return scheduled


def _halt_turn(session_id="sess-1", provider="openai-codex"):
    """Simulate Hermes for one failed attempt: classifier transform, then observer."""
    verdict = plugin.on_transform_api_error_classification(
        provider=provider, model="gpt-5", status_code=429, error_message="usage_limit_reached")
    plugin.on_api_request_error(provider=provider, session_id=session_id,
                                reason=(verdict or {}).get("reason", "rate_limit"), platform="cli")
    return verdict


def test_transform_returns_fail_fast_verdict_and_stashes(env):
    verdict = plugin.on_transform_api_error_classification(
        provider="zai", model="glm-4.6", status_code=429, error_message="1308 Usage limit reached")
    assert verdict["reason"] == "billing"
    assert verdict["retryable"] is False
    assert verdict["should_fallback"] is True
    assert "zai" in plugin._PENDING


def test_transform_declines_short_throttle(env, monkeypatch):
    monkeypatch.setattr(plugin.limits, "fetch_windows_cached",
                        lambda p: [Window("Session", 10.0, time.time() + 3600)])
    assert plugin.on_transform_api_error_classification(
        provider="anthropic", model="claude-opus", status_code=429,
        error_message="per-minute rate limit") is None
    assert plugin._PENDING == {}


def test_error_hook_schedules_once_while_resume_pending(env):
    _halt_turn()
    _halt_turn()  # second halt while the first resume is still in the future
    assert len(env) == 1
    assert env[0][2] == "openai-codex"  # provider reaches the generated script
    marker = json.loads((plugin._STATE_DIR / "sess-1.json").read_text(encoding="utf-8"))
    assert marker["provider"] == "openai-codex" and marker["job_id"] == "job-1"


def test_error_hook_reschedules_after_stale_marker(env):
    _halt_turn()
    marker = plugin._STATE_DIR / "sess-1.json"
    data = json.loads(marker.read_text(encoding="utf-8"))
    data["reset_at"] = "2000-01-01T00:00:00+00:00"  # earlier resume already fired
    marker.write_text(json.dumps(data), encoding="utf-8")
    _halt_turn()  # resumed session hit the limit again -> next window
    assert len(env) == 2


def test_error_hook_ignores_without_stash(env):
    plugin.on_api_request_error(provider="openai-codex", session_id="s", reason="billing")
    assert env == []


def test_error_hook_requires_our_verdict_reason(env):
    plugin.on_transform_api_error_classification(
        provider="openai-codex", model="gpt-5", status_code=429, error_message="usage_limit_reached")
    plugin.on_api_request_error(provider="openai-codex", session_id="s", reason="rate_limit")
    assert env == []


def test_failed_scheduling_writes_no_marker(env, monkeypatch):
    monkeypatch.setattr(plugin, "schedule_resume_job", lambda **kw: None)
    _halt_turn()
    assert not (plugin._STATE_DIR / "sess-1.json").exists()


def test_register_registers_both_hooks():
    seen = []

    class Ctx:
        def register_hook(self, name, cb):
            seen.append(name)

    plugin.register(Ctx())
    assert seen == ["transform_api_error_classification", "api_request_error"]
