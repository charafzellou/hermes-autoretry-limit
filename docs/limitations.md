# Limitations

Things this plugin deliberately does not do, and known edge cases. Written
plainly so nobody is surprised in production.

## Anthropic-only, on purpose

The classifier check in `_is_claude_usage_limit()` hardcodes
`provider == "anthropic"`. This is not an oversight — Claude Pro/Max's
usage model (periodic 5-hour/weekly windows with a knowable `resets_at`,
exposed through a dedicated OAuth usage API) is fairly unique among
providers. Generalizing this to "any provider's rate limit" would mean:

- Different providers expose reset-time information differently (some not
  at all, some only as a `Retry-After` header on the failed request
  itself).
- Different providers' "billing" vs. "transient rate limit" boundary isn't
  drawn the same way Hermes draws it for Anthropic.
- A generic version would need a per-provider reset-time strategy, which
  is a bigger design than this plugin currently attempts.

If you want to extend this to another provider, the natural place is
`_anthropic_reset_epoch()` in `__init__.py` — add a per-provider branch
that knows how to find that provider's reset time, and loosen the
`provider ==` check in `_is_claude_usage_limit()`.

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

## Doesn't retry the scheduling itself

If `schedule_resume_job()` fails for any reason (e.g. `hermes cron create`
errors out, the scripts directory isn't writable, etc.), the failure is
logged and the plugin gives up silently for that halt — it does not retry
scheduling, and it does not fall back to a different mechanism. Nor does
it retry the *resume* if that itself somehow fails (though the generated
script does print a clear manual-resume command in that case — see
`schedule.py`'s `_RESUME_SCRIPT_TEMPLATE`).

This is a deliberate simplicity choice (YAGNI): the plugin either manages
to queue exactly one resume attempt per halt, or it doesn't and you fall
back to noticing and resuming manually, same as before you installed it.
It never becomes a source of noisy repeated failures.

## No config surface

There's no config file, no environment variable knobs, no way to change
the retry buffer (`RESUME_BUFFER_SECONDS` in `schedule.py`) without editing
the source. If you want this configurable, it's a small, welcome PR — but
it wasn't built in from day one because there was no concrete need for it
yet.

## Multiple sessions halted by the same account-wide limit

If you have several Hermes sessions all halted by the same Claude Pro/Max
account-wide usage cap, each schedules its own independent resume job,
all landing at roughly the same reset time (since it's the same account
window). When they fire, they'll all try to resume within moments of each
other. This should be fine in practice — they're independent conversations
and will serialize naturally through Hermes's own credential/rate-limit
handling for that account — but if you run many parallel sessions, a burst
of near-simultaneous resumes could itself briefly re-trip a rate limit on
Anthropic's side (a much shorter-lived one than the original usage-window
cap). Not observed in testing, but worth knowing about.

## Windows-first testing

Developed and verified end-to-end on Windows (including the real
`hermes cron create` / `hermes cron list` / `hermes cron remove` round
trip). Nothing in the code is Windows-specific — paths go through
`pathlib.Path` and `hermes_constants.get_hermes_home()` rather than
hardcoded separators — but macOS/Linux has not yet been explicitly
verified. If you hit a platform-specific issue, please file it.

## Doesn't detect halts retroactively

Plugins load at Hermes session start. If you had a session halt by a
Claude usage limit *before* you installed/enabled this plugin, nothing
will retroactively notice that halt — you'll need to resume it manually
this one time. The plugin only starts watching once it's loaded into a
running session.

## Trusts Hermes's own classifier

The `reason == "rate_limit"` check leans entirely on Hermes's built-in
error classification being correct. If a future Hermes version changes how
it classifies Claude usage-limit errors (e.g. renames the reason, or
starts classifying some genuine usage-limit cases as `billing`), this
plugin's detection would silently under- or over-fire until updated to
match. This is an accepted tradeoff for not re-implementing Claude's error
message parsing ourselves — see
[`how-it-works.md`](how-it-works.md#2-filtering-is-this-actually-a-claude-usage-limit-halt)
for the reasoning.
