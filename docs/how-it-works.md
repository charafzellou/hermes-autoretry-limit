# How it works

This document walks through the full mechanism, in the order events
actually happen, with references to the exact hook names and API calls
involved.

## 1. The trigger: `api_request_error`

Hermes Agent ships a documented plugin-hook system (see Hermes's own
`website/docs/user-guide/features/hooks.md`, or `hermes hooks` in a running
install). One of those hooks, `api_request_error`, fires **once per failed
provider attempt** — i.e. every time a call to Claude, OpenAI, etc. comes
back with an error, before Hermes decides what to do about it (retry,
back off, rotate credentials, fall back to another provider, or give up).

The hook is observer-only: registered callbacks receive the failure's
details as keyword arguments, and whatever they return is ignored. Hermes's
own retry/backoff/fallback logic runs independently — this plugin cannot
accidentally interfere with it, only watch.

Relevant payload fields (see `hermes_agent/agent/turn_recovery.py` and the
hooks doc for the authoritative list):

| field | meaning |
|---|---|
| `provider` | which provider the failed call was to, e.g. `"anthropic"` |
| `model` | the model that was being called |
| `session_id` | the Hermes session this turn belongs to |
| `reason` | Hermes's own classification of the failure (see below) |
| `retryable` | whether Hermes's classifier thinks retrying could work |
| `retry_count` / `max_retries` | which attempt this is, out of how many |
| `platform` | `cli`, `tui`, `telegram`, etc. |

`claude-usage-autoresume` registers a callback,
`on_api_request_error()` in `__init__.py`, against this hook.

## 2. Filtering: is this actually a Claude usage-limit halt?

Not every `api_request_error` matters. The callback applies three filters,
implemented in `_is_claude_usage_limit()`:

1. **Provider must be `anthropic`.** This plugin is Claude-specific by
   design — see [`limitations.md`](limitations.md) for why it doesn't try
   to generalize to every provider's rate limits.

2. **Reason must be `rate_limit`, not `billing`.** Hermes's own error
   classifier (`agent/error_classifier.py` inside Hermes) already does the
   hard work of telling a *transient, periodic* usage cap (Claude Pro/Max's
   5-hour/weekly windows — classified `rate_limit`) apart from a *real*
   billing wall (dead card, revoked OAuth, no credits — classified
   `billing` or `billing_unverified`). We deliberately don't touch
   `billing` failures: auto-resuming there would just retry into the same
   wall and waste a cron cycle. A human needs to fix billing; a timer can
   fix a usage window.

   This is also why the plugin doesn't parse Claude's error text itself —
   Hermes has already done that classification work, upstream, more
   robustly than a regex in a plugin ever could.

3. **Must be the final attempt of the turn** (`retry_count >= max_retries`).
   `api_request_error` can fire multiple times per turn as Hermes retries;
   we only care about the attempt that actually gives up, otherwise we'd
   schedule a resume for a turn that's about to succeed on its own retry
   two seconds later.

If all three hold, this is treated as a genuine "you're out of Claude Pro/Max
usage until the window resets" halt.

## 3. Finding the real reset time: Claude's own usage API

Once a qualifying halt is detected, the plugin does **not** try to extract
a reset time from the failed turn's error text. Instead it calls Hermes's
own account-usage helper:

```python
from agent.account_usage import fetch_account_usage
snapshot = fetch_account_usage("anthropic")
```

This function (already part of Hermes, used by Hermes's own `/usage`
command) authenticates with your stored Anthropic OAuth credentials and
calls `https://api.anthropic.com/api/oauth/usage` directly — Claude's own
account API, the same data Claude Code and claude.ai's own usage indicator
read from. The response includes multiple usage windows (`five_hour`,
`seven_day`, `seven_day_opus`, `seven_day_sonnet`), each with a
`resets_at` timestamp.

`_anthropic_reset_epoch()` takes the **soonest** reset time among the
returned windows — the earliest resets_at is the one that actually gates
whether your next turn will succeed, since a shorter, sooner-expiring
window is what tripped the block.

This is strictly more accurate than trusting whatever hint (if any) rode
along in the failed turn's error body — it's a live, authoritative account
query, not a guess.

## 4. Scheduling the resume: Hermes's own cron system

With a concrete `reset_at` timestamp in hand, `schedule.py` does two
things:

1. **Writes a tiny per-session resume script** to `$HERMES_HOME/scripts/`
   (Hermes's own convention for cron-runnable scripts):

   ```python
   # $HERMES_HOME/scripts/autoresume_<session_id>.py
   subprocess.run(["hermes", "chat", "-q", "Continue where we left off.",
                    "--oneshot", "--resume", SESSION_ID, "-Q"])
   ```

2. **Registers a one-shot cron job** via Hermes's documented CLI:

   ```bash
   hermes cron create "<reset_at + 60s>" \
     --name claude-usage-autoresume-<session_id> \
     --script autoresume_<session_id>.py \
     --no-agent --deliver local --repeat 1
   ```

   `--no-agent` means the job doesn't spend an LLM call of its own — it
   just runs the script and delivers its stdout. The script itself invokes
   a **new** `hermes chat` process bound to the original session id, which
   is what actually continues your conversation.

   The 60-second buffer (`RESUME_BUFFER_SECONDS` in `schedule.py`) exists
   because the usage window's `resets_at` is measured server-side; by the
   time our scheduled job fires, a little wall-clock drift is possible.
   Firing a minute late is harmless; firing a few seconds early would just
   retrigger the same "usage limit reached" error and waste the cron run.

The plugin shells out to the `hermes cron create` CLI rather than importing
Hermes's internal `cron/jobs.py` functions directly — the CLI is Hermes's
stable, documented surface, and staying on it means this plugin should keep
working across Hermes upgrades without needing to track internal refactors.

## 5. Deduping: one resume per halt

Before scheduling, the plugin checks for a marker file at
`state/<session_id>.json`. If a resume is already scheduled for this
session, it does nothing — this prevents queuing duplicate cron jobs if
`api_request_error` happens to fire more than once for what is really the
same halt (or if you hit the limit again before the first scheduled resume
has fired).

The marker records `session_id`, `reset_at`, the cron `job_id`, and when it
was scheduled — useful for debugging (see
[`faq-and-troubleshooting.md`](faq-and-troubleshooting.md)).

## 6. What happens when the cron job fires

At the scheduled time, Hermes's cron scheduler (which requires
`hermes gateway install` or `hermes gateway run` to be running — see
[`installation.md`](installation.md)) runs the generated resume script.
That script starts a brand-new `hermes chat --resume <session_id>`
process, which:

- Loads the session's full prior conversation history from Hermes's
  session store.
- Sends one new turn ("Continue where we left off.") to nudge the model to
  pick back up.
- Since your Claude Pro/Max usage window has now genuinely reset, this
  call succeeds where the original one failed.

If for any reason the resume attempt itself fails (e.g. the window hasn't
actually reset, or some other error), the script prints a clear message
telling you the exact command to resume manually
(`hermes --resume <session_id>`) — it never silently swallows a failure.

## Summary diagram

```
 Hermes turn fails (Claude 429, usage limit)
              │
              ▼
   api_request_error hook fires  ──────────────► other Hermes hook
              │                                    subscribers (untouched)
              ▼
   _is_claude_usage_limit()?
      provider == anthropic
      reason   == rate_limit   (not billing)
      final retry attempt
              │ yes
              ▼
   fetch_account_usage("anthropic")
      → Claude's own OAuth usage API
      → soonest window reset_at
              │
              ▼
   already scheduled for this session?  ── yes ──► no-op
              │ no
              ▼
   write $HERMES_HOME/scripts/autoresume_<sid>.py
   hermes cron create <reset_at+60s> --script ... --no-agent
              │
              ▼
   marker written: state/<sid>.json
              │
              ▼
        ... time passes ...
              │
              ▼
   cron scheduler fires the job (needs gateway running)
              │
              ▼
   hermes chat --resume <sid>  (brand-new process)
              │
              ▼
   your session continues on its own
```
