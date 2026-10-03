# Installation

## Prerequisites

1. **Hermes Agent** installed and working (`hermes --help` runs).
2. **Anthropic OAuth login** — this plugin only does anything for sessions
   running on a Claude Pro/Max subscription via OAuth, not a raw API key.
   Check with:

   ```bash
   hermes auth list anthropic
   ```

   If you're not logged in: `hermes auth add anthropic`.

3. **Hermes's cron scheduler running.** This is the single most common
   reason people think the plugin "isn't working" — the plugin will
   correctly detect the halt and queue the resume job, but nothing fires
   it unless a scheduler is ticking. Check:

   ```bash
   hermes cron status
   ```

   If it says the gateway/scheduler isn't running:

   ```bash
   hermes gateway install     # installs as a background service (recommended)
   # or, for a quick test:
   hermes gateway run         # runs in the foreground, blocks your terminal
   ```

## Step 1 — Find your Hermes home

```bash
hermes doctor | grep -i home
```

On most installs this is `~/.hermes`; on this project's original dev
machine (Windows) it resolved to `C:\Users\<you>\AppData\Local\hermes`.
Always trust what `hermes doctor` reports over any hardcoded guess — it
accounts for `$HERMES_HOME` overrides and multi-profile setups.

## Step 2 — Clone the plugin in

The repo root **is** the plugin directory, and Hermes derives the plugin's
lookup key from the directory name — so clone it into a directory named
exactly `claude-usage-autoresume`:

```bash
git clone https://github.com/<you>/claude-usage-autoresume \
  "$HERMES_HOME/plugins/claude-usage-autoresume"
```

(A symlink to a local clone also works, if you'd rather develop against
your working copy and have changes picked up without re-cloning:

```bash
ln -s "$(pwd)" "$HERMES_HOME/plugins/claude-usage-autoresume"
```

Note: symlinks may behave differently on Windows depending on your shell —
`mklink /D` in cmd.exe, or just copy the directory if in doubt.)

## Step 3 — Enable it

```bash
hermes plugins enable claude-usage-autoresume
```

Expected output:

```
✓ Plugin claude-usage-autoresume enabled. Takes effect on next session.
```

## Step 4 — Verify it loads cleanly

```bash
hermes plugins doctor claude-usage-autoresume
```

Expected output:

```
Plugin Doctor:
<path>\claude-usage-autoresume
  manifest: claude-usage-autoresume 0.2.0 (standalone)
  OK: runtime discovery, manifest parsing, import, and registration passed
  registrations: 0 tool(s), 2 hook(s)
```

If you instead see a `WARN: registration adds hook '...' not listed in
provides_hooks` — that means `plugin.yaml`'s `provides_hooks:` list and the
`ctx.register_hook(...)` call in `__init__.py` have drifted apart. In the
shipped version they match (`api_request_error` in both places); if you've
edited the plugin, keep them in sync.

## Step 5 — Confirm the plugin appears as enabled

```bash
hermes plugins list --plain --no-bundled
```

You should see a line like:

```
enabled      user     0.2.0    claude-usage-autoresume
```

## Step 6 — Run the unit tests (optional but recommended)

```bash
cd claude-usage-autoresume
uvx pytest tests -q -p no:cacheprovider
```

Expected output: `32 passed`. These tests don't touch the network or the
filesystem outside a temp directory — pure-function tests of the
classification logic, the hook wiring, and the generated cron script.

Optionally, verify against the real Hermes classifier and your live
provider accounts (read-only):

```bash
"$LOCALAPPDATA/hermes/hermes-agent/venv/Scripts/python.exe" tests/hermes_integration_check.py
# expected: four case lines + INTEGRATION OK

"$LOCALAPPDATA/hermes/hermes-agent/venv/Scripts/python.exe" tests/probe_usage.py anthropic openai-codex zai
# expected: each provider's usage windows and the blocking reset time
```

## Step 7 — Start using it

Plugins take effect for sessions started **after** you enable them. Start
a new Hermes session normally:

```bash
hermes
```

Nothing changes about your day-to-day usage. The plugin sits quietly until
you actually hit a genuine Claude Pro/Max usage-limit halt — at which point
it should log something like this to Hermes's log (or wherever your
platform surfaces plugin log output):

```
claude-usage-autoresume: scheduled resume of session <id> at <reset_at> (job <job_id>)
```

You can confirm a resume got queued at any time with:

```bash
hermes cron list
```

and inspect the dedupe marker at:

```
$HERMES_HOME/plugins/claude-usage-autoresume/state/<session_id>.json
```

## Uninstalling

```bash
hermes plugins disable claude-usage-autoresume
```

or delete the plugin directory outright. If you have any resume jobs still
queued that you'd like to cancel:

```bash
hermes cron list                    # find the job id/name
hermes cron remove <job_id>
```

Deleting the plugin directory does **not** automatically cancel any
already-scheduled cron jobs — remove those separately if you don't want
them to fire.
