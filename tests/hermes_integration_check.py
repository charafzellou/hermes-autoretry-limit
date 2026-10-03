"""Real-Hermes check — run with Hermes's interpreter, NOT pytest:

    "$LOCALAPPDATA/hermes/hermes-agent/venv/Scripts/python.exe" tests/hermes_integration_check.py

Feeds synthetic 429s through the INSTALLED Hermes error classifier with this
plugin's transform hook wired in, and asserts Hermes's turn loop would fail fast
(no api_max_retries x 600 s waits) for usage-window halts while a plain
throttle keeps Hermes's normal retries. No network: usage lookups are stubbed.
"""
import importlib.util
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(os.environ["LOCALAPPDATA"]) / "hermes" / "hermes-agent"))

_spec = importlib.util.spec_from_file_location(
    "autoresume_plugin", ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
plugin = importlib.util.module_from_spec(_spec)
sys.modules["autoresume_plugin"] = plugin
_spec.loader.exec_module(plugin)

import hermes_cli.plugins as hermes_plugins  # noqa: E402
from agent.error_classifier import RETRYABLE_CLIENT_REASONS, classify_api_error  # noqa: E402


def _invoke_hook(name, **kwargs):
    if name == "transform_api_error_classification":
        return [plugin.on_transform_api_error_classification(**kwargs)]
    return []


hermes_plugins.invoke_hook = _invoke_hook  # get_plugin_error_classification looks this up at call time


class FakeAPIError(Exception):
    def __init__(self, message, status_code, body):
        super().__init__(message)
        self.status_code, self.body = status_code, body


def would_fail_fast(c):  # mirrors is_client_error in agent/turn_api_error.py
    return (not c.retryable) and (not c.should_compress) and c.reason not in RETRYABLE_CLIENT_REASONS


exhausted = [plugin.limits.Window("Session", 100.0, time.time() + 3 * 3600)]
healthy = [plugin.limits.Window("Session", 15.0, time.time() + 3 * 3600)]
CASES = [
    ("anthropic", "claude-opus-4-1", "Error code: 429 - rate_limit_error",
     {"type": "error", "error": {"type": "rate_limit_error", "message": "This request would exceed your account's rate limit."}},
     exhausted, True),
    ("openai-codex", "gpt-5-codex", "usage_limit_reached",
     {"error": {"type": "usage_limit_reached", "message": "The usage limit has been reached", "resets_in_seconds": 7200}},
     exhausted, True),
    ("zai", "glm-4.6", "Error code: 429 - Usage limit reached for 5 hour.",
     {"error": {"code": "1308", "message": "Usage limit reached for 5 hour. Your limit will reset at 2026-10-04 03:00:00"}},
     [], True),
    ("anthropic", "claude-opus-4-1", "rate_limit_error: per-minute rate limit",
     {"type": "error", "error": {"type": "rate_limit_error", "message": "Number of requests has exceeded your per-minute rate limit"}},
     healthy, False),
]

for provider, model, message, body, windows, expected in CASES:
    plugin.limits._CACHE.clear()
    plugin.limits.fetch_windows_cached = lambda p, w=windows: w
    c = classify_api_error(FakeAPIError(message, 429, body), provider=provider, model=model)
    got = would_fail_fast(c)
    print(f"{provider:13} fail_fast={got!s:5} reason={c.reason.value:10} msg={c.message[:60]!r}")
    assert got is expected, (provider, c)
print("INTEGRATION OK")
