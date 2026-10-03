# Architecture

## File map

This repository **is** the plugin directory — the repo root is what you
clone into `$HERMES_HOME/plugins/hermes-autoretry-limit/`.

```
hermes-autoretry-limit/   (repo root == plugin dir)
├── __init__.py            Hooks: registers transform_api_error_classification
│                           (fail-fast verdict) + api_request_error (schedules
│                           the resume), dedupes via state/ markers.
├── limits.py              Provider knowledge: alias tables, per-provider
│                            usage-window fetchers (incl. Z.AI quota API),
│                            blocking-window selection, error-text signatures.
├── schedule.py              Scheduling: writes the per-session watchdog
│                            resume script (55-min hop chain) and calls
│                            `hermes cron create`.
├── plugin.yaml               Plugin manifest Hermes reads at discovery time.
├── tests/
│   ├── conftest.py             Loads the repo root as `autoresume_plugin`.
│   ├── test_limits.py          Unit tests: aliases, Z.AI payload parsing,
│   │                             blocking-window selection, classify_halt.
│   ├── test_hooks.py           Unit tests: both hook callbacks + dedupe.
│   ├── test_schedule.py        Unit tests: cron argv, generated script.
│   ├── hermes_integration_check.py  Real-Hermes classifier round-trip
│   │                             (run with Hermes's venv python, not pytest).
│   ├── probe_usage.py          Read-only live probe of all three usage APIs.
│   └── dryrun_schedule.py      Real `hermes cron create` dry-run + cleanup.
├── docs/                          This documentation set.
└── state/                          Created at runtime for dedupe markers;
                                     gitignored, not in the repo.
```

Total: ~450 lines of plugin code across three files. Deliberately small — see
`limitations.md` for what was left out on purpose.

## Why three files, not one

The split follows the plugin's three concerns:

- `__init__.py` — **hooks**: depends on Hermes's plugin-hook system
  (`register_hook`) and stashes halts between the two hook callbacks.
  Read-only, observation-side.
- `limits.py` — **provider knowledge**: the only file that knows about
  Anthropic/Codex/Z.AI APIs. Depends on Hermes's account-usage helper and
  (lazily) httpx. Pure-ish: everything except the fetchers is unit-testable
  without network.
- `schedule.py` — **scheduling**: depends only on the **`hermes cron
  create` CLI** and the filesystem (`$HERMES_HOME/scripts/`) — a write-side
  concern, and the only place this plugin has any side effect on your
  Hermes install.

Keeping them separate means you can unit-test the classification logic
(`tests/test_limits.py`) and hook wiring (`tests/test_hooks.py`) without
ever touching the filesystem or shelling out to `hermes cron create` —
which is exactly what the test suite does.

## Data flow

See the diagram in `how-it-works.md` (Summary diagram section) for the
full step-by-step flow. At a glance:

```
Hermes core                        this plugin                        Hermes CLI / OS
────────────                       ───────────                        ───────────────
turn fails (429)
  │
  ├─► transform_api_error_classification hook ──► on_transform_api_error_classification()
  │       │                                          │
  │       │                                          ├─► limits.classify_halt()  (pure fn:
  │       │                                          │      aliases, signatures, windows)
  │       │                                          ├─► limits.fetch_windows_cached() ──►  Anthropic OAuth usage API
  │       │                                          │      (Hermes helper / Z.AI quota API)   Codex backend usage API
  │       │                                          │                                         Z.AI quota API
  │       │  verdict: billing / retryable=False      │
  │       ▼  (Hermes stops retrying)                 └─► stash halt in _PENDING[provider]
  │
  ├─► api_request_error hook ──► on_api_request_error()
                                       │
                                       ├─► marker check  (state/*.json; stale = reschedule)
                                       │
                                       └─► schedule_resume_job()
                                                │
                                                ├─► write watchdog script ────►  $HERMES_HOME/scripts/
                                                │
                                                └─► subprocess.run(
                                                      ["hermes","cron","create",...]
                                                    )                ────►  hermes cron create
                                                                              (Hermes's cron store)

  ... later, when the cron scheduler ticks ...

Hermes cron scheduler  ──►  runs autoresume_<sid>.py
  (needs gateway running)      │
                               ├─► window still closed? ──► hermes cron create next hop (+55 min), exit
                               │
                               └─► hermes chat --resume <sid>   (brand-new process;
                                            │                    re-arms the chain on next halt)
                                            ▼
                                  your session continues
```

## External dependencies (all internal to Hermes)

This plugin has **zero third-party Python dependencies** — everything it
imports is either Python stdlib (`json`, `logging`, `subprocess`,
`datetime`, `pathlib`) or a function already shipped inside Hermes Agent
itself:

| Import | Where it comes from | What it's used for |
|---|---|---|
| `agent.account_usage.fetch_account_usage` | Hermes core (`hermes-agent/agent/account_usage.py`) | Querying Anthropic's OAuth usage API and the Codex backend usage API for reset times |
| `hermes_cli.runtime_provider.resolve_runtime_provider` | Hermes core | Resolving the Z.AI key/base URL (Coding Plan endpoint) |
| `httpx` (lazy import) | Hermes's own dependency | Calling the Z.AI `/api/monitor/usage/quota/limit` quota endpoint |
| `agent.retry_utils.reset_delay_from_message` | Hermes core | Parsing reset hints from error text (fallback path) |
| `hermes_constants.get_hermes_home` | Hermes core | Resolving `$HERMES_HOME` correctly across platforms/profiles |
| `hermes cron create` (subprocess) | Hermes CLI | Scheduling the one-shot resume job |
| `hermes chat --resume` (subprocess, inside the generated script) | Hermes CLI | Actually resuming the session |

Because everything routes through Hermes's own documented CLI and public
helper functions — never private internals like `cron/jobs.py`'s
functions directly — this plugin should keep working across normal Hermes
upgrades without needing to track Hermes's internal refactors.

## Plugin manifest (`plugin.yaml`)

```yaml
name: hermes-autoretry-limit
version: "0.2.0"
description: >
  ...
author: "..."
provides_hooks:
  - transform_api_error_classification
  - api_request_error
```

`provides_hooks` is what Hermes's plugin doctor
(`hermes plugins doctor hermes-autoretry-limit`) checks against the hooks
your `register()` function actually registers, to catch a manifest that's
drifted out of sync with the code.

## State on disk

The only persistent state this plugin creates is:

- `state/<session_id>.json` — one marker per session with a resume
  scheduled. Contents: `session_id`, `provider`, `source` (how the reset
  time was found), `reset_at` (ISO 8601), `job_id` (the
  Hermes cron job id), `scheduled_at`, `platform`. A marker only blocks
  new scheduling while its `reset_at` is still in the future — once the
  resume time has passed it is stale and the next halt reschedules. Safe
  to delete by hand if you want to force a re-schedule.
- `$HERMES_HOME/scripts/autoresume_<session_id>.py` — the generated
  watchdog resume script Hermes's cron job actually executes. NOT safe to
  delete while a usage window is still closed: each run re-schedules its
  own next check (+55 min). Once the window has reopened and the session
  resumed, it's dead weight and can be removed.

Neither of these is tracked in git — `state/` is created on demand at
runtime and gitignored, so a fresh clone contains no runtime state at all.
