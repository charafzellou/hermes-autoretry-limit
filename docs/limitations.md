# Limitations

Things this plugin deliberately does not do, and known edge cases. Written
plainly so nobody is surprised in production.

## Supported providers (three, on purpose)

Detection is keyed on an explicit alias table (`_PROVIDER_ALIASES` in
`limits.py`) rather than "any provider's rate limit". Supported today:

- **Anthropic** — Claude Pro/Max OAuth. Windows from
  `https://api.anthropic.com/api/oauth/usage` (via Hermes).
- **OpenAI Codex** — ChatGPT-plan OAuth. Windows from the ChatGPT backend
  usage API (via Hermes).
- **Z.AI** — GLM Coding Plan (API keys; the pay-as-you-go route has no
  windows). Windows from `/api/monitor/usage/quota/limit`.

Every other provider's rate limits are ignored on purpose: providers expose
reset information differently (some not at all), and the "billing vs
transient usage window" boundary differs per provider. To add one, you need
exactly three things in `limits.py`: an alias entry in `_PROVIDER_ALIASES`,
an error-text pattern in `_TEXT_SIGNATURES`, and a fetcher branch in
`fetch_windows()` returning a list of `Window(label, used_percent,
reset_epoch)`.

The Z.AI quota endpoint is not officially documented; it is the same
endpoint the z.ai dashboard and community tools use. If Z.AI changes its
payload shape (the V3 "points" plans are the known risk), parsing degrades
to an empty list — detection then falls back to error-text signatures and
the 55-minute hop chain, which still works, just less precisely.

## Shows up as a billing-style error in the transcript

To stop Hermes's retry loop, the plugin classifies the halt as `billing`
(the only built-in reason that fails fast AND still rotates credentials /
tries fallback providers). Side effect: Hermes may print its billing
guidance text under the plugin's own message. Cosmetic, but don't be
alarmed by it — no money is involved; it's a subscription window.

## Only resumes the session, not the original process

Covered in the main README, but worth repeating here: for CLI/TUI
sessions, the original Hermes process has already exited or returned
control to you by the time the turn fails. This plugin starts a **new**
process (`hermes chat --resume <session_id>`) at reset time — it does not
and cannot "un-freeze" your original terminal window. If you were mid-way
through watching Hermes work in a terminal, that terminal will simply have
returned to your shell prompt; the resumed conversation happens in a
separate, new process.

For gateway-connected sessions (Telegram, Discord, etc.) this distinction
matters less, since those sessions are addressed by ID and don't have a
"the terminal window" concept — resuming there should feel more seamless.

## Requires a running cron scheduler

`hermes cron create` will happily accept and store the job even if no
scheduler is ticking — but nothing will actually fire it until one is.
This means installing the plugin alone is not sufficient; you also need
`hermes gateway install` (or `hermes gateway run`) so cron jobs are
actually serviced. See [`installation.md`](installation.md) and
[`faq-and-troubleshooting.md`](faq-and-troubleshooting.md).

## Retries forever — one API call per window

The infinite loop is the point, but know its cost: if an account is
permanently blocked in a way that still *looks* like a usage limit, the
plugin costs one failed request per window plus one quota-check per
55-minute hop, forever. To stop it:

```bash
hermes plugins disable claude-usage-autoresume
hermes cron list        # find leftover jobs
hermes cron remove <job_id>
```

A failure to *schedule* (e.g. `hermes cron create` errors out) is logged
and no marker is written, so the next halt retries scheduling — the loop
self-heals rather than giving up. A failed *resume attempt* prints the
exact manual-resume command (see `schedule.py`'s
`_RESUME_SCRIPT_TEMPLATE`); it never silently swallows a failure.

## No config surface

There's no config file, no environment variable knobs, no way to change
the retry buffer (`RESUME_BUFFER_SECONDS`), the hop interval
(`HOP_MINUTES`), or the exhaustion threshold (`EXHAUSTED_PERCENT`) without
editing the source. They live at the top of `schedule.py` / `limits.py`
on purpose — small, visible, tweakable constants.

## Multiple sessions halted by the same account-wide limit

If you have several Hermes sessions all halted by the same account-wide
usage cap, each schedules its own independent resume job, all landing at
roughly the same reset time (since it's the same account window). When
they fire, they'll all try to resume within moments of each other. This
should be fine in practice — they're independent conversations and will
serialize naturally through Hermes's own credential/rate-limit handling —
but a burst of near-simultaneous resumes could itself briefly re-trip a
short-lived rate limit. Not observed in testing, but worth knowing about.

Related: with a multi-credential pool, Hermes may rotate to a healthy
credential and keep working — the already-scheduled resume still fires
later and sends one unnecessary "Continue where we left off." to that
session. Harmless; cancelling the job on recovery is not implemented
(YAGNI).

## Windows-first testing

Developed and verified end-to-end on Windows (including the real
`hermes cron create` / `hermes cron list` / `hermes cron remove` round
trip). Nothing in the code is Windows-specific — paths go through
`pathlib.Path` and `hermes_constants.get_hermes_home()` rather than
hardcoded separators — but macOS/Linux has not yet been explicitly
verified. If you hit a platform-specific issue, please file it.

## Doesn't detect halts retroactively

Plugins load at Hermes session start. If you had a session halt by a
usage limit *before* you installed/enabled this plugin, nothing will
retroactively notice that halt — you'll need to resume it manually
this one time. The plugin only starts watching once it's loaded into a
running session.

## Leans on Hermes internals, carefully

Detection reads Hermes's usage helper (`agent.account_usage`) and reset-time
parser (`agent.retry_utils.reset_delay_from_message`), and registers the
`transform_api_error_classification` hook — documented or stable surfaces,
but internal enough that a future Hermes refactor could move them. The
failure mode is graceful: an import error inside the transform hook logs
and declines, Hermes keeps classifying normally, and you're no worse off
than without the plugin. The cron interaction goes through the `hermes
cron` CLI only.
