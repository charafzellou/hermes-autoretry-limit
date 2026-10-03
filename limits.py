"""Provider knowledge for claude-usage-autoresume.

Which providers have subscription usage windows, how to read those windows,
which window is actually blocking, and when the session can resume.
Hermes imports are lazy (inside functions) so this module and its unit tests
import under a bare Python interpreter.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urlparse

logger = logging.getLogger("plugin.claude_usage_autoresume")

# Hermes provider slug or alias -> canonical slug this plugin supports.
_PROVIDER_ALIASES = {
    "anthropic": "anthropic",
    "openai-codex": "openai-codex", "codex": "openai-codex",
    "zai": "zai", "glm": "zai", "z-ai": "zai", "z.ai": "zai", "zhipu": "zai",
}

# A window at/above this utilisation is blocking (APIs round: 100 % may be 99.6).
EXHAUSTED_PERCENT = 99.0
# Never schedule a resume sooner than this (no hot loop on a past/near reset).
MIN_RESUME_DELAY_SECONDS = 120
# Known usage-limit halt but no reset time anywhere: first re-check in 30 min.
# The resume script's 55-minute hop chain takes over from there.
FALLBACK_RESUME_DELAY_SECONDS = 30 * 60
_CACHE_TTL_SECONDS = 60

# Error-text signatures of a subscription usage-window halt (case-insensitive).
# Used only when the usage API cannot confirm an exhausted window.
_TEXT_SIGNATURES = {
    "anthropic": re.compile(r"usage limit|limit reached|out of extra usage", re.I),
    "openai-codex": re.compile(r"usage_limit_reached|usage limit", re.I),
    "zai": re.compile(r"\b13(?:08|10)\b|usage limit|limit exhausted", re.I),
}

# Z.AI quota rows: (unit, number) -> label. unit 3 = hours, unit 6 = weeks.
_ZAI_WINDOW_LABELS = {(3, 5): "5-hour", (6, 1): "Weekly"}
_ZAI_QUOTA_PATH = "/api/monitor/usage/quota/limit"


@dataclass(frozen=True)
class Window:
    label: str
    used_percent: Optional[float]
    reset_epoch: Optional[float]


@dataclass(frozen=True)
class Halt:
    provider: str
    resume_epoch: float
    source: str  # "usage_api" | "error_text" | "fallback"


def normalize_provider(provider: Any) -> Optional[str]:
    return _PROVIDER_ALIASES.get(str(provider or "").strip().lower())


def matches_usage_limit_text(provider: str, message: str) -> bool:
    pattern = _TEXT_SIGNATURES.get(provider)
    return bool(pattern and pattern.search(message or ""))


def _window_applies_to_model(label: str, model: str) -> bool:
    """Anthropic reports per-family weekly windows ("Opus week", "Sonnet week");
    those only gate turns when the session's model is in that family."""
    low_label, low_model = label.lower(), (model or "").lower()
    for family in ("opus", "sonnet"):
        if family in low_label:
            return family in low_model
    return True


def blocking_reset_epoch(windows: Iterable[Window], *, now: float, model: str = "") -> Optional[float]:
    """When the session can run again: the LATEST future reset among exhausted
    windows. If the weekly window is full, the 5-hour reset is useless, so the
    weekly reset wins. None = no applicable window is exhausted."""
    resets = [
        w.reset_epoch for w in windows
        if w.used_percent is not None and w.used_percent >= EXHAUSTED_PERCENT
        and w.reset_epoch is not None and w.reset_epoch > now
        and _window_applies_to_model(w.label, model)
    ]
    return max(resets) if resets else None


def parse_zai_limits(payload: Any) -> List[Window]:
    """Z.AI ``/api/monitor/usage/quota/limit`` body -> 5-hour / weekly windows."""
    data = payload.get("data") if isinstance(payload, dict) else None
    rows = data.get("limits") if isinstance(data, dict) else None
    windows: List[Window] = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict) or row.get("type") not in ("TOKENS_LIMIT", "CREDIT_LIMIT"):
            continue
        label = _ZAI_WINDOW_LABELS.get((row.get("unit"), row.get("number")))
        if label is None:
            continue
        pct, reset_ms = row.get("percentage"), row.get("nextResetTime")
        windows.append(Window(
            label=label,
            used_percent=float(pct) if isinstance(pct, (int, float)) else None,
            reset_epoch=float(reset_ms) / 1000.0 if isinstance(reset_ms, (int, float)) and reset_ms > 0 else None,
        ))
    return windows


def zai_quota_url(base_url: str) -> str:
    """Quota URL on the official origin matching the configured region (the GLM
    Coding Plan's dedicated endpoint api.z.ai/api/coding/paas/v4, or BigModel CN).
    Custom or unknown hosts fall back to api.z.ai so the key never goes anywhere else."""
    host = (urlparse(base_url or "").hostname or "").lower()
    origin = "https://open.bigmodel.cn" if host.endswith("bigmodel.cn") else "https://api.z.ai"
    return origin + _ZAI_QUOTA_PATH


def _windows_from_hermes(provider: str) -> List[Window]:
    from agent.account_usage import fetch_account_usage
    snapshot = fetch_account_usage(provider)
    if not snapshot or not snapshot.windows:
        return []
    return [Window(w.label, w.used_percent, w.reset_at.timestamp() if w.reset_at else None)
            for w in snapshot.windows]


def _fetch_zai_windows() -> List[Window]:
    import httpx
    from hermes_cli.runtime_provider import resolve_runtime_provider
    runtime = resolve_runtime_provider(requested="zai")
    key = str(runtime.get("api_key") or "").strip()
    if not key:
        return []
    url = zai_quota_url(str(runtime.get("base_url") or ""))
    with httpx.Client(timeout=10.0, follow_redirects=False) as client:
        # Bearer is the form every working community client uses; raw key is legacy.
        for auth in (f"Bearer {key}", key):
            resp = client.get(url, headers={"Authorization": auth, "Accept": "application/json",
                                            "Accept-Language": "en-US,en"})
            if resp.status_code in (401, 403):
                continue
            resp.raise_for_status()
            return parse_zai_limits(resp.json())
    return []


def fetch_windows(provider: str) -> List[Window]:
    if provider == "zai":
        return _fetch_zai_windows()
    return _windows_from_hermes(provider)


_CACHE: Dict[str, Tuple[float, List[Window]]] = {}


def fetch_windows_cached(provider: str) -> List[Window]:
    """``fetch_windows`` behind a 60 s cache. Never raises: failure -> []."""
    hit = _CACHE.get(provider)
    if hit and time.time() - hit[0] < _CACHE_TTL_SECONDS:
        return hit[1]
    try:
        windows = fetch_windows(provider)
    except Exception:
        logger.warning("claude-usage-autoresume: usage lookup for %s failed", provider, exc_info=True)
        windows = []
    _CACHE[provider] = (time.time(), windows)
    return windows


def message_reset_epoch(message: str, *, now: float) -> Optional[float]:
    """Reset time named in the error text ("resets in 4hr", resets_in_seconds...),
    via Hermes's own parser. None outside Hermes or when nothing is named."""
    try:
        from agent.retry_utils import reset_delay_from_message
        seconds = reset_delay_from_message(message or "")
    except Exception:
        return None
    return now + float(seconds) if seconds and seconds > 0 else None


def classify_halt(
    *, provider: Any, model: Any, status_code: Any, message: str,
    now: Optional[float] = None,
    fetch: Optional[Callable[[str], List[Window]]] = None,
) -> Optional[Halt]:
    """Is this failed attempt a subscription usage-window halt? If so, when to resume."""
    canonical = normalize_provider(provider)
    if canonical is None:
        return None
    text_match = matches_usage_limit_text(canonical, message)
    if status_code != 429 and not text_match:
        return None
    now = time.time() if now is None else now
    fetch = fetch or fetch_windows_cached  # resolved at call time so tests can monkeypatch
    try:
        windows = fetch(canonical)
    except Exception:
        windows = []
    epoch, source = blocking_reset_epoch(windows, now=now, model=str(model or "")), "usage_api"
    if epoch is None:
        if not text_match:
            return None  # plain throttle: Hermes's own retry/backoff handles it
        epoch, source = message_reset_epoch(message, now=now), "error_text"
        if epoch is None:
            epoch, source = now + FALLBACK_RESUME_DELAY_SECONDS, "fallback"
    return Halt(canonical, max(epoch, now + MIN_RESUME_DELAY_SECONDS), source)
