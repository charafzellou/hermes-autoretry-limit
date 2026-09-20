"""Unit tests for claude-usage-autoresume's detection predicate.

No live network / cron calls — pure-function tests for _is_claude_usage_limit.
Run directly: `python test_detect.py` (prints OK on success) or via pytest.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import importlib.util

_spec = importlib.util.spec_from_file_location(
    "claude_usage_autoresume_under_test",
    Path(__file__).resolve().parent.parent / "__init__.py",
)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
_is_claude_usage_limit = _mod._is_claude_usage_limit


def test_matches_final_anthropic_rate_limit():
    assert _is_claude_usage_limit(
        provider="anthropic", reason="rate_limit", retryable=True,
        retry_count=3, max_retries=3,
    ) is True


def test_ignores_other_provider():
    assert _is_claude_usage_limit(
        provider="openrouter", reason="rate_limit", retryable=True,
        retry_count=3, max_retries=3,
    ) is False


def test_ignores_billing_reason():
    assert _is_claude_usage_limit(
        provider="anthropic", reason="billing", retryable=False,
        retry_count=3, max_retries=3,
    ) is False


def test_ignores_billing_unverified_reason():
    assert _is_claude_usage_limit(
        provider="anthropic", reason="billing_unverified", retryable=False,
        retry_count=3, max_retries=3,
    ) is False


def test_ignores_mid_retry_attempt():
    # Not the final attempt yet — Hermes's own retry may still recover.
    assert _is_claude_usage_limit(
        provider="anthropic", reason="rate_limit", retryable=True,
        retry_count=1, max_retries=3,
    ) is False


def test_provider_case_insensitive():
    assert _is_claude_usage_limit(
        provider="Anthropic", reason="RATE_LIMIT", retryable=True,
        retry_count=2, max_retries=2,
    ) is True


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  ok: {t.__name__}")
    print("OK")
