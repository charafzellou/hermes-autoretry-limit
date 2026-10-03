"""hermes-autoretry-limit — detection half.

Two Hermes plugin hooks fire, in this order and in the same process, for every
failed provider attempt (agent/turn_api_error.py):

1. ``transform_api_error_classification`` (Transform; runs first inside Hermes's
   error classifier). If the failure is a subscription usage-window halt for
   Anthropic (Claude Pro/Max), OpenAI Codex (ChatGPT plan) or Z.AI (GLM Coding
   Plan) — see limits.classify_halt — we return a fail-fast verdict so Hermes
   stops instead of spending api_max_retries x 600 s of Retry-After waits on a
   window that is closed for hours. Credential rotation and fallback providers
   still run first.
2. ``api_request_error`` (Observer). Consumes the halt stashed by step 1 and
   schedules a one-shot ``hermes cron`` resume for when the BLOCKING window
   (5-hour or weekly, whichever ends later) reopens.

Retrying forever: the resumed ``hermes chat --resume`` process loads this plugin
too. If the window is still (or again) closed, the cycle repeats. The per-session
marker only blocks while an already-scheduled resume is still in the future. And
the generated resume script never gets caught mid-resume by Hermes's 1-hour
script timeout: if the window is still closed when it fires, it schedules the
next check ~55 minutes out and exits (see schedule.py).
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

try:
    from . import limits
    from .schedule import RESUME_BUFFER_SECONDS, schedule_resume_job
except ImportError:
    # This file lives at the repo root, which is also the pytest rootdir: pytest
    # creates a Package node for any directory with an __init__.py and imports
    # the file as a bare top-level module named "__init__", where relative
    # imports have no parent package. Hermes itself always loads the plugin as a
    # real package (hermes_plugins.<slug> with __path__ set), so only tests hit
    # this branch. limits.py and schedule.py are deliberately self-contained
    # (no relative imports), so loading them standalone is safe.
    import importlib.util
    import sys

    def _load_standalone(stem: str):
        name = f"_autoresume_standalone_{stem}"
        if name not in sys.modules:
            spec = importlib.util.spec_from_file_location(
                name, Path(__file__).resolve().parent / f"{stem}.py")
            assert spec is not None and spec.loader is not None
            mod = importlib.util.module_from_spec(spec)
            sys.modules[name] = mod
            spec.loader.exec_module(mod)
        return sys.modules[name]

    limits = _load_standalone("limits")
    _schedule = _load_standalone("schedule")
    RESUME_BUFFER_SECONDS = _schedule.RESUME_BUFFER_SECONDS
    schedule_resume_job = _schedule.schedule_resume_job

logger = logging.getLogger("plugin.claude_usage_autoresume")

_STATE_DIR = Path(__file__).resolve().parent / "state"

# The reason our transform hands Hermes; the observer only acts on it.
_FAIL_FAST_REASON = "billing"  # outside Hermes's RETRYABLE_CLIENT_REASONS -> no retry loop

# canonical provider -> (stashed_at, Halt). Written by the transform hook and
# consumed milliseconds later by api_request_error for the same attempt.
_PENDING: Dict[str, Tuple[float, limits.Halt]] = {}
_PENDING_TTL_SECONDS = 60


def _marker_path(session_id: str) -> Path:
    _STATE_DIR.mkdir(parents=True, exist_ok=True)
    safe_id = "".join(c for c in session_id if c.isalnum() or c in "-_") or "unknown"
    return _STATE_DIR / f"{safe_id}.json"


def _resume_still_pending(marker: Path, now: float) -> bool:
    """True while a previously scheduled resume for this session has not fired yet."""
    try:
        data = json.loads(marker.read_text(encoding="utf-8"))
        reset_at = datetime.fromisoformat(data["reset_at"]).timestamp()
        return reset_at + RESUME_BUFFER_SECONDS > now
    except Exception:
        return False  # missing/corrupt marker never blocks rescheduling


def on_transform_api_error_classification(
    *, provider: Any = None, model: Any = None, status_code: Any = None,
    error_message: Any = None, **_kwargs: Any,
) -> Optional[Dict[str, Any]]:
    """Return a fail-fast verdict for a usage-window halt, else None (Hermes
    classifies normally). Never raises."""
    try:
        halt = limits.classify_halt(provider=provider, model=model, status_code=status_code,
                                    message=str(error_message or ""))
        if halt is None:
            return None
        _PENDING[halt.provider] = (time.time(), halt)
        when = datetime.fromtimestamp(halt.resume_epoch).astimezone().strftime("%Y-%m-%d %H:%M")
        return {
            "reason": _FAIL_FAST_REASON,
            "retryable": False,
            "should_rotate_credential": True,
            "should_fallback": True,
            "message": (f"{halt.provider} subscription usage limit reached — "
                        f"hermes-autoretry-limit will resume this session after {when}."),
        }
    except Exception:
        logger.exception("hermes-autoretry-limit: transform hook failed (fail-open)")
        return None


def on_api_request_error(
    *, provider: Any = None, session_id: Any = None, reason: Any = None,
    platform: Any = None, **_kwargs: Any,
) -> None:
    """Observer: schedule the resume for a halt our transform just classified."""
    try:
        if str(reason or "").strip().lower() != _FAIL_FAST_REASON:
            return
        canonical = limits.normalize_provider(provider)
        stashed = _PENDING.pop(canonical, None) if canonical else None
        if not stashed or time.time() - stashed[0] > _PENDING_TTL_SECONDS:
            return
        halt = stashed[1]
        sid = str(session_id or "").strip()
        if not sid:
            logger.warning("hermes-autoretry-limit: no session_id on failure payload, cannot schedule resume")
            return
        marker = _marker_path(sid)
        if _resume_still_pending(marker, time.time()):
            logger.debug("hermes-autoretry-limit: resume already pending for session %s", sid)
            return
        resume_at = datetime.fromtimestamp(halt.resume_epoch, tz=timezone.utc)
        job_id = schedule_resume_job(session_id=sid, resume_at=resume_at, provider=halt.provider)
        if not job_id:
            return  # already logged; no marker, so the next halt retries scheduling
        marker.write_text(json.dumps({
            "session_id": sid, "provider": halt.provider, "source": halt.source,
            "reset_at": resume_at.isoformat(), "job_id": job_id,
            "scheduled_at": datetime.now(timezone.utc).isoformat(), "platform": platform,
        }), encoding="utf-8")
        logger.info("hermes-autoretry-limit: %s halt (%s) — resume of session %s scheduled at %s (job %s)",
                    halt.provider, halt.source, sid, resume_at.isoformat(), job_id)
    except Exception:
        logger.exception("hermes-autoretry-limit: on_api_request_error failed (fail-open)")


def register(ctx) -> None:
    ctx.register_hook("transform_api_error_classification", on_transform_api_error_classification)
    ctx.register_hook("api_request_error", on_api_request_error)
