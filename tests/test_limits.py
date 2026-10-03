from autoresume_plugin import limits
from autoresume_plugin.limits import Window

NOW = 1_800_000_000.0
H = 3600.0


def test_normalize_provider_aliases():
    assert limits.normalize_provider("Anthropic") == "anthropic"
    assert limits.normalize_provider("openai-codex") == "openai-codex"
    assert limits.normalize_provider("glm") == "zai"
    assert limits.normalize_provider(" ZAI ") == "zai"


def test_normalize_provider_unknown():
    assert limits.normalize_provider("openrouter") is None
    assert limits.normalize_provider(None) is None


ZAI_V2 = {"data": {"level": "max", "limits": [
    {"type": "TIME_LIMIT", "unit": 5, "number": 1, "percentage": 0, "nextResetTime": 1788073095998},
    {"type": "TOKENS_LIMIT", "unit": 3, "number": 5, "percentage": 100, "nextResetTime": 1787056863927},
    {"type": "TOKENS_LIMIT", "unit": 6, "number": 1, "percentage": 20, "nextResetTime": 1787641095989},
]}}
ZAI_CREDIT = {"data": {"level": "pro", "limits": [
    {"type": "CREDIT_LIMIT", "unit": 3, "number": 5, "percentage": 0},
    {"type": "CREDIT_LIMIT", "unit": 6, "number": 1, "percentage": 0, "nextResetTime": 1787649214999},
]}}


def test_parse_zai_limits_v2_payload():
    assert limits.parse_zai_limits(ZAI_V2) == [
        Window("5-hour", 100.0, 1787056863.927),
        Window("Weekly", 20.0, 1787641095.989),
    ]


def test_parse_zai_limits_credit_payload():
    assert limits.parse_zai_limits(ZAI_CREDIT) == [
        Window("5-hour", 0.0, None),
        Window("Weekly", 0.0, 1787649214.999),
    ]


def test_parse_zai_limits_garbage():
    assert limits.parse_zai_limits(None) == []
    assert limits.parse_zai_limits({"data": {"limits": "nope"}}) == []
    assert limits.parse_zai_limits({"data": {"limits": [{"type": "TOKENS_LIMIT", "unit": 9, "number": 9}]}}) == []


def test_zai_quota_url_regions():
    assert limits.zai_quota_url("https://api.z.ai/api/coding/paas/v4") == "https://api.z.ai/api/monitor/usage/quota/limit"
    assert limits.zai_quota_url("https://open.bigmodel.cn/api/coding/paas/v4") == "https://open.bigmodel.cn/api/monitor/usage/quota/limit"
    # Custom host never receives the key.
    assert limits.zai_quota_url("https://evil.example.com/v4") == "https://api.z.ai/api/monitor/usage/quota/limit"


def test_blocking_only_five_hour_exhausted():
    ws = [Window("Current session", 100.0, NOW + 2 * H), Window("Current week", 40.0, NOW + 90 * H)]
    assert limits.blocking_reset_epoch(ws, now=NOW) == NOW + 2 * H


def test_blocking_weekly_wins_when_both_exhausted():
    ws = [Window("Current session", 100.0, NOW + 2 * H), Window("Current week", 100.0, NOW + 90 * H)]
    assert limits.blocking_reset_epoch(ws, now=NOW) == NOW + 90 * H


def test_blocking_weekly_only():
    ws = [Window("Session", 12.0, NOW + 2 * H), Window("Weekly", 99.5, NOW + 50 * H)]
    assert limits.blocking_reset_epoch(ws, now=NOW) == NOW + 50 * H


def test_blocking_none_exhausted():
    ws = [Window("Session", 80.0, NOW + 2 * H), Window("Weekly", None, NOW + 50 * H)]
    assert limits.blocking_reset_epoch(ws, now=NOW) is None


def test_blocking_ignores_past_resets():
    assert limits.blocking_reset_epoch([Window("Session", 100.0, NOW - 10)], now=NOW) is None


def test_blocking_ignores_other_model_family_window():
    ws = [Window("Current session", 100.0, NOW + H), Window("Opus week", 100.0, NOW + 100 * H)]
    assert limits.blocking_reset_epoch(ws, now=NOW, model="claude-sonnet-4-5") == NOW + H
    assert limits.blocking_reset_epoch(ws, now=NOW, model="claude-opus-4-1") == NOW + 100 * H


def _fetch(windows):
    return lambda provider: windows


def test_text_signatures():
    assert limits.matches_usage_limit_text("openai-codex", "429 {'error': {'type': 'usage_limit_reached'}}")
    assert limits.matches_usage_limit_text("zai", "Error code: 429 - {'error': {'code': '1308', 'message': 'Usage limit reached for 5 hour.'}}")
    assert limits.matches_usage_limit_text("anthropic", "Claude usage limit reached")
    assert not limits.matches_usage_limit_text("anthropic", "Rate limited: 50 requests per minute")
    assert not limits.matches_usage_limit_text("zai", "code 11308 something")


def test_classify_halt_usage_api():
    halt = limits.classify_halt(provider="openai-codex", model="gpt-5", status_code=429, message="rate limited",
                                now=NOW, fetch=_fetch([Window("Session", 100.0, NOW + 3 * H)]))
    assert halt == limits.Halt("openai-codex", NOW + 3 * H, "usage_api")


def test_classify_halt_text_fallback_when_api_empty():
    halt = limits.classify_halt(provider="zai", model="glm-4.6", status_code=429,
                                message="{'code': '1308', 'message': 'Usage limit reached for 5 hour.'}",
                                now=NOW, fetch=_fetch([]))
    assert halt == limits.Halt("zai", NOW + limits.FALLBACK_RESUME_DELAY_SECONDS, "fallback")


def test_classify_halt_short_throttle_returns_none():
    assert limits.classify_halt(
        provider="anthropic", model="claude-opus-4-1", status_code=429,
        message="rate_limit_error: Number of requests has exceeded your per-minute rate limit",
        now=NOW, fetch=_fetch([Window("Current session", 40.0, NOW + H)])) is None


def test_classify_halt_unsupported_provider():
    assert limits.classify_halt(provider="openrouter", model="x", status_code=429, message="usage limit",
                                now=NOW, fetch=_fetch([Window("S", 100.0, NOW + H)])) is None


def test_classify_halt_clamps_min_delay():
    halt = limits.classify_halt(provider="anthropic", model="claude-opus", status_code=429, message="",
                                now=NOW, fetch=_fetch([Window("Current session", 100.0, NOW + 5)]))
    assert halt.resume_epoch == NOW + limits.MIN_RESUME_DELAY_SECONDS


def test_classify_halt_fetch_exception_falls_back_to_text():
    def boom(provider):
        raise RuntimeError("network down")
    halt = limits.classify_halt(provider="openai-codex", model="gpt-5", status_code=429,
                                message="usage_limit_reached", now=NOW, fetch=boom)
    assert halt.source == "fallback"
