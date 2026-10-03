# How it works

This document walks through the full mechanism, in the order events
actually happen, with references to the exact hook names and API calls
involved.

## 1. The trigger: two hooks, in order

Hermes Agent ships a documented plugin-hook system (see Hermes's own
`website/docs/user-guide/features/hooks.md`, or `hermes hooks` in a running
install). For every failed provider attempt, two hooks fire — in this order,
in the same process and thread (`agent/turn_api_error.py`):

1. `transform_api_error_classification` (Transform) — runs FIRST, inside
   Hermes's error classifier, before Hermes decides how to recover. A
   callback returns `None` (decline; Hermes classifies normally) or a dict:
   `{"reason", "retryable", "should_compress", "should_rotate_credential",
   "should_fallback", "message"}`.
2. `api_request_error` (Observer) — fires after classification with the
   failure's details as keyword arguments. Relevant payload fields:

   | field | meaning |
   |---|---|
   | `provider` | which provider the failed call was to, e.g. `"anthropic"` |
   | `model` | the model that was being called |
   | `session_id` | the Hermes session this turn belongs to |
   | `reason` | Hermes's classification of the failure |
   | `retryable` | whether Hermes's classifier thinks retrying could work |
   | `retry_count` / `max_retries` | which attempt this is, out of how many |
   | `platform` | `cli`, `tui`, `telegram`, etc. |

   Whatever the callback returns is ignored — Hermes's own
   retry/backoff/fallback logic runs independently of the observer.

`claude-usage-autoresume` registers against both hooks:
`on_transform_api_error_classification()` and `on_api_request_error()` in
`__init__.py`.

## 2. Decision: is this actually a usage-window halt?

Every failed attempt goes through `limits.classify_halt()` (in `limits.py`):

1. **Provider check.** The payload's `provider` (e.g. `anthropic`,
   `openai-codex`, `zai`/`glm`/`z.ai`/`zhipu`) is normalized through
   `_PROVIDER_ALIASES`. Anything else → decline (`None`).

2. **Status check.** HTTP 429, or error text matching the provider's
   usage-limit signature (`_TEXT_SIGNATURES`: Anthropic "usage limit",
   Codex `usage_limit_reached`, Z.AI codes `1308`/`1310`). Otherwise →
   decline (a plain 5xx or a bad request is none of our business).

3. **Usage API check.** The provider's usage windows are fetched (60 s
   cache; failures → empty list). If an applicable window is exhausted
   (>= 99 % used), this is a **halt**, and the resume time is the LATEST
   reset among those windows — the *blocking* window. If your weekly
   window is full, the 5-hour reset is useless (the turn would fail again),
   so the weekly reset wins. Anthropic's per-family weekly windows
   ("Opus week", "Sonnet week") only count when the session's model is in
   that family.

4. **Text fallback.** If the usage API shows nothing exhausted but the error
   text matches a signature, the reset time is parsed from the message via
   Hermes's own `agent.retry_utils.reset_delay_from_message`
   ("resets in 4hr", `resets_in_seconds`, ...). Nothing parseable → first
   re-check in 30 minutes (the resume script's 55-minute hop chain takes
   over from there).

5. **Nothing matches → decline.** A short per-minute throttle is left to
   Hermes's normal retry/backoff, exactly as without the plugin.

When the transform declines, Hermes classifies normally and nothing else
happens. When it accepts, it returns a fail-fast verdict:

```python
{"reason": "billing", "retryable": False, "should_rotate_credential": True,
 "should_fallback": True,
 "message": "<provider> subscription usage limit reached — "
            "claude-usage-autoresume will resume this session after HH:MM."}
```

`billing` is deliberate: it is the one built-in reason that (a) is outside
Hermes's `RETRYABLE_CLIENT_REASONS`, so `retryable: False` actually stops
the retry loop instead of burning 3 x 600 s Retry-After waits, and (b) still
lets Hermes rotate credentials and try fallback providers first. The cost is
cosmetic: Hermes may print its billing-guidance text under our message.

The accepted halt is also stashed in memory, keyed by provider, for the
observer hook that fires milliseconds later.

## 3. Finding the real reset time: each provider's own usage API

`limits.fetch_windows()` reads the windows for the canonical provider:

- **`anthropic`** — Hermes's own helper
  (`from agent.account_usage import fetch_account_usage`),
  which authenticates with your stored Anthropic OAuth credentials and calls
  `https://api.anthropic.com/api/oauth/usage`. Windows: "Current session"
  (5-hour), "Current week", "Opus week", "Sonnet week" — each with
  `used_percent` and `reset_at`.
- **`openai-codex`** — the same Hermes helper, which calls the ChatGPT
  backend usage API with your Codex credentials. Windows: "Session"
  (5-hour), "Weekly".
- **`zai`** — no Hermes fetcher exists, so the plugin calls
  `GET https://api.z.ai/api/monitor/usage/quota/limit` (China:
  `https://open.bigmodel.cn/...`) itself, with your Coding Plan key as
  `Authorization: Bearer <key>` (raw-key fallback for legacy). The
  `data.limits[]` rows map `unit=3, number=5` to the 5-hour window and
  `unit=6, number=1` to the weekly window; `nextResetTime` is epoch
  milliseconds. `TIME_LIMIT` rows are the monthly MCP-tool budget and are
  ignored (they don't gate chat). The quota URL is always on the same
  official origin as your configured Coding Plan endpoint — a custom or
  unknown host falls back to `api.z.ai`, so your key never goes anywhere else.

This is strictly more accurate than trusting whatever hint (if any) rode
along in the failed turn's error body — it's a live, authoritative account
query, not a guess.

## 4. Scheduling the resume: Hermes's own cron system, with a 55-minute watchdog

With a concrete resume time, `schedule.py` does two things:

1. **Writes a tiny per-session resume script** to `$HERMES_HOME/scripts/`
   (Hermes's own convention for cron-runnable scripts). The script does NOT
   blindly resume. At fire time it:

   - Fetches the provider's usage windows (via the installed plugin's
     `limits` module).
   - **Window open** → runs `hermes chat -q "Continue where we left off."
     --oneshot --resume <session_id> -Q` synchronously and exits. The
     resumed session loads this plugin too, so the whole chain re-arms
     itself if the limit is hit again.
   - **Window closed or unknown** → creates the NEXT one-shot cron job
     ~55 minutes out and exits immediately.

   That hop behaviour is the watchdog: Hermes kills a `--no-agent` cron
   script's ENTIRE process tree after `cron.script_timeout_seconds`
   (default 3600 s) — an in-progress `hermes chat --resume` included. By
   keeping every script run to a few seconds and chaining 55-minute checks,
   no resume can ever be killed mid-turn, and the chain keeps polling until
   the window genuinely reopens (covering unknown reset times, server-side
   clock drift, or a provider re-capping you early).

2. **Registers the one-shot cron job** via Hermes's documented CLI:

   ```bash
   hermes cron create "<resume_at + 60s>" \
     --name claude-usage-autoresume-<session>-<fire_epoch> \
     --script autoresume_<session>.py \
     --no-agent --deliver local --failure-deliver local --repeat 1 \
     --interpreter <the plugin's own python>
   ```

   `--no-agent` means the job doesn't spend an LLM call of its own — it just
   runs the script and delivers its stdout. The 60-second buffer
   (`RESUME_BUFFER_SECONDS` in `schedule.py`) covers server-side clock
   drift: firing a minute late is harmless, firing early would just
   re-trigger the same "usage limit reached" error and waste the cron run.
   `--failure-deliver local` keeps a crashed hop from spamming FAILURE
   notices. Job names carry the fire time so every link of the chain is
   distinct in `hermes cron list`.

The plugin shells out to the `hermes cron create` CLI rather than importing
Hermes's internal `cron/jobs.py` functions directly — the CLI is Hermes's
stable, documented surface, and staying on it means this plugin should keep
working across Hermes upgrades without needing to track internal refactors.

## 5. Deduping and the infinite loop

The plugin keeps a marker file at `state/<session_id>.json`. Unlike a
one-shot "already scheduled" flag, the marker only blocks while its recorded
`reset_at` is still in the future. Once a scheduled resume has fired (or its
time has passed), the marker is stale: the next halt schedules a new resume.

That is the whole infinite loop:

- Session hits the limit → halt detected → resume scheduled at the blocking
  reset → Hermes stops immediately.
- At the reset, the script resumes the session.
- If the session's next turn hits the limit again (window re-exhausted, or a
  longer window is now the blocker), the marker is stale → new halt → next
  resume scheduled. Repeat forever, one failed API call per cycle.
- The 55-minute hop chain covers the gap where the reset time was unknown or
  wrong: it keeps checking until the window actually opens.

The marker records `session_id`, `provider`, the detection `source`,
`reset_at`, the cron `job_id`, and when it was scheduled — useful for
debugging (see [`faq-and-troubleshooting.md`](faq-and-troubleshooting.md)).

## 6. What happens when the window opens

At the scheduled time, Hermes's cron scheduler (which requires
`hermes gateway install` or `hermes gateway run` to be running — see
[`installation.md`](installation.md)) runs the generated resume script. The
window is open, so the script starts a brand-new
`hermes chat --resume <session_id>` process, which:

- Loads the session's full prior conversation history from Hermes's
  session store.
- Sends one new turn ("Continue where we left off.") to nudge the model to
  pick back up.
- Since the blocking window has now genuinely reset, this call succeeds
  where the original one failed.

If for any reason the resume attempt itself fails, the script prints a clear
message telling you the exact command to resume manually
(`hermes --resume <session_id>`) — it never silently swallows a failure.

## Summary diagram

```
 Hermes turn fails (429 usage limit)
              │
              ▼
 on_transform_api_error_classification
   limits.classify_halt()
      provider in {anthropic, openai-codex, zai}?
      status 429 or usage-limit text?
      usage API: any applicable window >= 99%?
              │ halt → stash + fail-fast verdict (billing)
              │        (else: decline, Hermes handles it)
              ▼
 on_api_request_error (observer)
   marker stale?  ── no ──► no-op
              │ yes
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
   cron scheduler fires the script (needs gateway running)
              │
              ▼
   usage API: window open? ── no ──► schedule next check +55 min, exit
              │ yes
              ▼
   hermes chat --resume <sid>  (brand-new process)
              │
              ▼
   your session continues — and re-arms the chain on the next halt
```
