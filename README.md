# hermes-autoretry-limit

A [Hermes Agent](https://github.com/NousResearch/hermes-agent) plugin that
auto-resumes your Hermes session after a subscription usage limit clears —
Claude Pro/Max (Anthropic OAuth), OpenAI Codex (ChatGPT plan), or Z.AI GLM
Coding Plan — with no manual re-prompting required.

If you drive Hermes with a provider subscription, you've hit this: mid-session,
the provider says you're out of usage (5-hour or weekly window), and your
Hermes session just... stops — after burning up to 3 retries x 600 s of
"Rate limited. Waiting 600s" on a window that is closed for hours. You have
to remember to come back and manually resume it once the window resets.

This plugin watches for exactly that failure, stops Hermes immediately
(no pointless retries), asks the provider's own usage API when your blocking
window actually reopens, and schedules Hermes to resume the session itself at
that moment via Hermes's built-in cron scheduler — repeating every window
until the turn succeeds.

## Status

Working, dog-fooded, but young. v0.2.0 adds multi-provider support (Anthropic,
OpenAI Codex, Z.AI GLM Coding Plan) and the infinite-resume chain.

Built and tested on Windows against a
current Hermes Agent install; not yet tested on macOS/Linux (should work
nothing in it is Windows-specific).

## Retry behaviour

- On a confirmed usage-window halt, the plugin makes Hermes stop after the
  first failed attempt — no 3 x 600 s retry loop. Credential rotation and
  fallback providers still get their chance first.
- The resume is scheduled for the **blocking** window: the latest reset among
  your exhausted windows. If the weekly window is full, waiting for the 5-hour
  reset would just fail again, so the weekly reset wins.
- The resumed session loads the plugin too; if it hits the limit again, the
  next resume is scheduled automatically. This repeats every window until the
  turn succeeds.
- If a scheduled resume fires while the window is still closed (unknown reset
  time, clock drift, provider re-cap), it re-schedules its own re-check
  ~55 minutes out and exits — every run stays seconds long, safely inside
  Hermes's one-hour cron script timeout, so no resume is ever killed
  mid-turn.
- To stop the loop entirely: `hermes plugins disable claude-usage-autoresume`
  and clear leftovers with `hermes cron list` / `hermes cron remove <job_id>`.

## What "automatic resume" actually means

For CLI/TUI sessions, the original Hermes process already exited (or
returned control to you) the moment the turn failed there's no live
process left to un-block.

So this plugin doesn't revive your old terminal window; it starts
a **new** `hermes` process at reset time that resumes the session's
saved conversation state (`hermes chat --resume <id>`). You'll
see a new Hermes invocation happen on its own, picking up your
conversation, not your original window unfreezing.

## Quick start

```bash
# 1. Copy (or symlink) this directory into your Hermes plugins folder.
#    Find your Hermes home with: hermes doctor | grep -i home
cp -r claude-usage-autoresume "$HERMES_HOME/plugins/"

# 2. Enable it
hermes plugins enable claude-usage-autoresume

# 3. Confirm it loads cleanly
hermes plugins doctor claude-usage-autoresume

# 4. Make sure the cron scheduler is actually running, or nothing will fire
hermes cron status

# if not the Hermes gateway is not running:
hermes gateway install        # background service (recommended)
# or: hermes gateway run      # foreground, for testing
```

Plugins take effect for sessions started **after** you enable them it
won't retroactively help a session that's already running.

## Requirements

- Hermes Agent with plugin support (`hermes plugins` command available).
- One of the supported subscription logins:
  - Claude Pro/Max: `hermes auth add anthropic` (OAuth, not an API key).
  - OpenAI Codex / ChatGPT plan: Codex OAuth (the same login Hermes's `/usage`
    command reads).
  - Z.AI GLM Coding Plan: `GLM_API_KEY` / `ZAI_API_KEY` (or
    `hermes auth add zai`), using the dedicated Coding Plan endpoint
    (`https://api.z.ai/api/coding/paas/v4`; China:
    `https://open.bigmodel.cn/api/coding/paas/v4`). Quota is read from
    `/api/monitor/usage/quota/limit` on the same region's origin.
- Hermes's cron scheduler actually running (`hermes gateway install` or
  `hermes gateway run`) otherwise scheduled resume jobs are queued but
  never fire.

## Disabling

```bash
hermes plugins disable claude-usage-autoresume
```

or delete the plugin directory entirely. Leftover scheduled resume jobs (if
any) can be removed with `hermes cron list` / `hermes cron remove <job_id>`.

## Docs

- [`docs/how-it-works.md`](docs/how-it-works.md) the full mechanism: hook
  timing, classification, the usage API call, cron scheduling, dedupe.
- [`docs/architecture.md`](docs/architecture.md) file-by-file breakdown
  and data flow diagram.
- [`docs/installation.md`](docs/installation.md) step-by-step setup,
  verification commands, and expected output.
- [`docs/faq-and-troubleshooting.md`](docs/faq-and-troubleshooting.md)
  common questions, failure modes, and how to debug them.
- [`docs/limitations.md`](docs/limitations.md) what this plugin
  deliberately does NOT do, and known edge cases.

## License

See [`LICENSE`](LICENSE).

## Contributing

Issues and PRs welcome.
