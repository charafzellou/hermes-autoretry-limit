"""claude-usage-autoresume — detection half.

Hooks Hermes's `api_request_error` plugin event (fires on every failed
provider attempt, see website/docs/user-guide/features/hooks.md). We only
act on the LAST attempt of a turn (retry_count >= max_retries) for the
Anthropic provider with a `rate_limit` classification — Hermes's own
classifier already separates a transient/periodic usage-limit 429
(`rate_limit`) from a real billing wall (`billing`), so we don't need to
parse error text ourselves; we just trust `reason`.

On a qualifying failure we ask Anthropic's own OAuth usage API (already
wired into Hermes at `agent.account_usage.fetch_account_usage`) for the
authoritative reset time of the five_hour usage window, and hand off to
`schedule.py` to queue a `hermes cron` one-shot resume job.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("plugin.claude_usage_autoresume")

# One-shot dedupe marker per session so a second failed attempt in the same
# halt (or a second halt before the scheduled resume fires) doesn't queue a
# duplicate cron job. Keyed by session_id.
_STATE_DIR = Path(__file__).resolve().parent / "state"

# Only these classifier reasons represent a genuinely transient, periodic
# usage cap. "billing"/"billing_unverified" mean the account needs a human
# (dead card, revoked OAuth, no credits) — auto-resuming would just retry
# and fail again, so they are deliberately excluded.
_TRANSIENT_USAGE_REASONS = {"rate_limit"}


def _marker_path(session_id: str) -> Path:
    _STATE_DIR.mkdir(parents=True, exist_ok=True)
    safe_id = "".join(c for c in session_id if c.isalnum() or c in "-_") or "unknown"
    return _STATE_DIR / f"{safe_id}.json"


def _is_final_attempt(retry_count: Any, max_retries: Any) -> bool:
    try:
        return int(retry_count) >= int(max_retries)
    except (TypeError, ValueError):
        # Missing/malformed counters: be conservative and treat as final,
        # since api_request_error can also fire once for a non-retryable error.
        return True


def _is_claude_usage_limit(*, provider: Any, reason: Any, retryable: Any,
                            retry_count: Any, max_retries: Any) -> bool:
    if str(provider or "").strip().lower() != "anthropic":
        return False
    if str(reason or "").strip().lower() not in _TRANSIENT_USAGE_REASONS:
        return False
    if not _is_final_attempt(retry_count, max_retries):
        return False  # a mid-retry 429 will likely succeed on its own retry
    return True


def _anthropic_reset_epoch() -> Optional[float]:
    """Authoritative reset time straight from Claude's own usage API — not
    a guess from this one failed turn's error body. Returns the soonest
    reset among windows that actually gate turns (five_hour first)."""
    try:
        from agent.account_usage import fetch_account_usage
        snapshot = fetch_account_usage("anthropic")
    except Exception:
        logger.exception("claude-usage-autoresume: usage fetch failed")
        return None
    if not snapshot or not snapshot.windows:
        return None
    candidates = [w.reset_at for w in snapshot.windows if w.reset_at is not None]
    if not candidates:
        return None
    return min(dt.timestamp() for dt in candidates)


def on_api_request_error(
    *, provider: Any = None, model: Any = None, session_id: Any = None,
    reason: Any = None, retryable: Any = None, retry_count: Any = None,
    max_retries: Any = None, platform: Any = None, **_kwargs: Any,
) -> None:
    """`api_request_error` plugin-hook callback. Observer-only: return value
    is ignored, so this never affects Hermes's own retry/backoff/fallback
    decisions. Failures inside this function must never propagate — Hermes
    isolates and logs plugin-hook exceptions, but we still guard defensively
    so a bug here can never look like a provider problem."""
    try:
        if not _is_claude_usage_limit(
            provider=provider, reason=reason, retryable=retryable,
            retry_count=retry_count, max_retries=max_retries,
        ):
            return
        sid = str(session_id or "").strip()
        if not sid:
            logger.warning("claude-usage-autoresume: no session_id on failure payload, cannot schedule resume")
            return
        marker = _marker_path(sid)
        if marker.exists():
            logger.debug("claude-usage-autoresume: resume already scheduled for session %s", sid)
            return
        reset_epoch = _anthropic_reset_epoch()
        if not reset_epoch:
            logger.warning("claude-usage-autoresume: no reset time available from usage API, skipping auto-resume for session %s", sid)
            return
        reset_dt = datetime.fromtimestamp(reset_epoch, tz=timezone.utc)

        from .schedule import schedule_resume_job
        job_id = schedule_resume_job(session_id=sid, resume_at=reset_dt)

        marker.write_text(json.dumps({
            "session_id": sid, "reset_at": reset_dt.isoformat(), "job_id": job_id,
            "scheduled_at": datetime.now(timezone.utc).isoformat(), "platform": platform,
        }))
        logger.info("claude-usage-autoresume: scheduled resume of session %s at %s (job %s)",
                    sid, reset_dt.isoformat(), job_id)
    except Exception:
        logger.exception("claude-usage-autoresume: on_api_request_error failed (fail-open)")


def register(ctx) -> None:
    ctx.register_hook("api_request_error", on_api_request_error)
