# claude-usage-autoresume

A [Hermes Agent](https://github.com/NousResearch/hermes-agent) plugin that
auto-resumes your Hermes session after a Claude Pro/Max usage limit
("5-hour limit reached") clears with no manual re-prompting required.

If you drive Hermes with a Claude Pro/Max subscription (Anthropic OAuth,
not an API key), you've hit this: mid-session, Claude says you're out of
usage for the next few hours, and your Hermes session just... stops. You
have to remember to come back and manually resume it once the window
resets.

This plugin watches for exactly that failure, asks Claude's own account API
when your usage window actually reopens, and schedules Hermes to resume the
session itself at that moment via Hermes's built-in cron scheduler.

## Status

Working, dog-fooded, but young.

Built and tested on Windows against a
current Hermes Agent install; not yet tested on macOS/Linux (should work
nothing in it is Windows-specific).

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
- Anthropic OAuth login (Claude Pro/Max subscription via `hermes auth add
anthropic`) this plugin is Anthropic-specific by design (see
  [`docs/limitations.md`](docs/limitations.md)).
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
