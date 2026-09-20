# Architecture

## File map

This repository **is** the plugin directory — the repo root is what you
clone into `$HERMES_HOME/plugins/claude-usage-autoresume/`.

```
claude-usage-autoresume/   (repo root == plugin dir)
├── __init__.py            Detection: registers the api_request_error hook,
│                           classifies the failure, looks up the reset time,
│                           dedupes, and hands off to schedule.py.
├── schedule.py              Scheduling: writes the per-session resume
│                            script and calls `hermes cron create`.
├── plugin.yaml               Plugin manifest Hermes reads at discovery time.
├── tests/
│   └── test_detect.py          Unit tests for the pure classification
│                                predicate (_is_claude_usage_limit).
├── docs/                          This documentation set.
└── state/                          Created at runtime for dedupe markers;
                                     gitignored, not in the repo.
```

Total: ~200 lines of plugin code across two files. Deliberately small — see
`limitations.md` for what was left out on purpose.

## Why two files, not one

`__init__.py` and `schedule.py` split along a clean seam: **detection**
(does this failure matter, and when does the window reopen) vs.
**scheduling** (turn a timestamp into a queued Hermes job). This mirrors
the plugin's own two dependencies:

- `__init__.py` depends on Hermes's **plugin-hook system**
  (`register_hook`) and **account-usage API**
  (`agent.account_usage.fetch_account_usage`) — both read-only,
  observation-side concerns.
- `schedule.py` depends only on the **`hermes cron create` CLI** and the
  filesystem (`$HERMES_HOME/scripts/`) — a write-side concern, and the only
  place this plugin has any side effect on your Hermes install.

Keeping them separate means you can unit-test the classification logic
(`tests/test_detect.py`) without ever touching the filesystem or shelling
out to `hermes cron create` — which is exactly what the test suite does.

## Data flow

See the diagram in `how-it-works.md` (Summary diagram section) for the
full step-by-step flow. At a glance:

```
Hermes core                    this plugin                  Hermes CLI / OS
────────────                   ───────────                  ───────────────
api_request_error hook  ──►  on_api_request_error()
                                     │
                                     ├─► _is_claude_usage_limit()  (pure fn)
                                     │
                                     ├─► fetch_account_usage()  ────►  Anthropic
                                     │      (Hermes's own helper)      OAuth usage API
                                     │
                                     ├─► marker check  (state/*.json)
                                     │
                                     └─► schedule_resume_job()
                                              │
                                              ├─► write script  ────►  $HERMES_HOME/scripts/
                                              │
                                              └─► subprocess.run(
                                                    ["hermes","cron","create",...]
                                                  )                ────►  hermes cron create
                                                                            (Hermes's cron store)

  ... later, when the cron scheduler ticks ...

Hermes cron scheduler  ──►  runs autoresume_<sid>.py  ────►  hermes chat --resume <sid>
  (needs gateway running)                                         (brand-new process)
```

## External dependencies (all internal to Hermes)

This plugin has **zero third-party Python dependencies** — everything it
imports is either Python stdlib (`json`, `logging`, `subprocess`,
`datetime`, `pathlib`) or a function already shipped inside Hermes Agent
itself:

| Import | Where it comes from | What it's used for |
|---|---|---|
| `agent.account_usage.fetch_account_usage` | Hermes core (`hermes-agent/agent/account_usage.py`) | Querying Claude's OAuth usage API for reset times |
| `hermes_constants.get_hermes_home` | Hermes core | Resolving `$HERMES_HOME` correctly across platforms/profiles |
| `hermes cron create` (subprocess) | Hermes CLI | Scheduling the one-shot resume job |
| `hermes chat --resume` (subprocess, inside the generated script) | Hermes CLI | Actually resuming the session |

Because everything routes through Hermes's own documented CLI and public
helper functions — never private internals like `cron/jobs.py`'s
functions directly — this plugin should keep working across normal Hermes
upgrades without needing to track Hermes's internal refactors.

## Plugin manifest (`plugin.yaml`)

```yaml
name: claude-usage-autoresume
version: "0.1.0"
description: >
  ...
author: "..."
provides_hooks:
  - api_request_error
```

`provides_hooks` is what Hermes's plugin doctor
(`hermes plugins doctor claude-usage-autoresume`) checks against the hooks
your `register()` function actually registers, to catch a manifest that's
drifted out of sync with the code.

## State on disk

The only persistent state this plugin creates is:

- `state/<session_id>.json` — one marker per session with a resume
  scheduled. Contents: `session_id`, `reset_at` (ISO 8601), `job_id` (the
  Hermes cron job id), `scheduled_at`, `platform`. Purely a dedupe guard;
  safe to delete by hand if you want to force a re-schedule (though
  normally you'd just let the existing scheduled job run).
- `$HERMES_HOME/scripts/autoresume_<session_id>.py` — the generated
  one-shot resume script Hermes's cron job actually executes. Safe to
  delete after the job has fired; nothing re-reads it.

Neither of these is tracked in git — `state/` is created on demand at
runtime and gitignored, so a fresh clone contains no runtime state at all.
