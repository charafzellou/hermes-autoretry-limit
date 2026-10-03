# FAQ & Troubleshooting

## "I hit a usage limit but nothing got scheduled"

Check, in order:

1. **Is the plugin actually enabled?**

   ```bash
   hermes plugins list --plain --no-bundled | grep claude-usage-autoresume
   ```

   Should say `enabled`. If it says `not enabled`, run
   `hermes plugins enable claude-usage-autoresume` and start a **new**
   session — plugins only load at session start, so an already-running
   session won't pick up a just-enabled plugin.

2. **Did the transform hook decline the failure?**

   The plugin only acts when `limits.classify_halt()` accepts the failure:
   a supported provider (anthropic / openai-codex / zai), HTTP 429 or
   usage-limit error text, AND a usage API window at >= 99 % (or a
   parseable reset hint in the error text). A short per-minute throttle or
   a healthy-window 429 is deliberately left to Hermes's normal retry. The
   plugin logs declines at debug level under `plugin.claude_usage_autoresume`.

3. **Was the turn recovered by something else?**

   The fail-fast verdict still lets Hermes rotate credentials / fall back
   to other providers first. If a fallback provider succeeded, the turn
   continued and there's nothing to schedule — working as intended.

4. **Check the logs directly.** The plugin logs under the logger name
   `plugin.claude_usage_autoresume`. Depending on your Hermes logging
   config, look in Hermes's log output for lines starting with
   `claude-usage-autoresume:`.

## "It scheduled a job but it never fired"

This is almost always the cron scheduler not running:

```bash
hermes cron status
```

If it reports the gateway/scheduler isn't running, install/start it:

```bash
hermes gateway install
# or, to test immediately without installing as a service:
hermes gateway run
```

`hermes cron create` will happily queue a job even with no scheduler
running — the job just sits there until something ticks it. This is
expected Hermes cron behavior, not a bug in this plugin.

## "How do I check what's currently scheduled?"

```bash
hermes cron list
```

Look for a job named `claude-usage-autoresume-<session-id-prefix>`.

## "How do I cancel a scheduled resume?"

```bash
hermes cron list                # find the job id
hermes cron remove <job_id>
```

Also delete the matching marker under
`$HERMES_HOME/plugins/claude-usage-autoresume/state/<session_id>.json` if
you don't want the plugin to consider that session "already handled" — but
note this only matters if you expect to hit the same limit again for the
same session; there's no harm leaving a stale marker around.

## "Will this resume EVERY halted session, or just the one I was in?"

Every session where a qualifying halt occurred gets its own independent
scheduled resume, keyed by `session_id`. If you have several sessions
halted by the same account-wide usage cap (any supported provider), each
gets its own cron job, all resolving to roughly the same blocking reset
time (since it's the same account/window). They will fire independently
and each resume its own conversation — see [`limitations.md`](limitations.md)
for a note on what happens if several fire at almost the same moment.

## "Does this cost extra tokens or API calls?"

- The usage-window lookup (`limits.fetch_windows_cached`) calls the
  provider's usage endpoint (Anthropic OAuth usage API, Codex backend usage
  API, or Z.AI `/api/monitor/usage/quota/limit`) — account metadata calls,
  not model inference calls, cached for 60 s. No extra token spend.
- The cron job itself runs `--no-agent` (see
  [`how-it-works.md`](how-it-works.md#4-scheduling-the-resume-hermess-own-cron-system-with-a-55-minute-watchdog))
  — the scheduled job's own execution doesn't call the LLM. While a window
  is closed, each 55-minute hop costs one quota-API call, not an LLM call.
- The resume it triggers (`hermes chat --resume <id>`) sends one new turn
  ("Continue where we left off.") — this is a normal Hermes turn and will
  use tokens/usage exactly like any message you'd type yourself.

## "Does it work with API keys instead of OAuth?"

For Anthropic and OpenAI Codex: no — those need the subscription OAuth
login, because the usage-window APIs are subscription features (an API-key
billing wall has no periodic window to wait for; auto-resuming it would
just retry into the same failure).

For Z.AI: the opposite — it works with your GLM **Coding Plan** API key
(the same key Hermes already uses for inference), because the quota
endpoint reads that key. A pay-as-you-go Z.AI key returns no windows and
is simply never detected as a halt.

## "Can I use this with other providers?"

Three are supported today: Anthropic (Claude Pro/Max), OpenAI Codex
(ChatGPT plan), and Z.AI (GLM Coding Plan). Anything else is ignored by
design. Adding one needs exactly three edits in `limits.py` — see
[`limitations.md`](limitations.md).

## "The plugin doctor shows a WARN about provides_hooks"

```
WARN: registration adds hook 'transform_api_error_classification' not listed in provides_hooks
```

Means `plugin.yaml`'s `provides_hooks:` list doesn't match what
`register()` actually calls `ctx.register_hook()` with in `__init__.py`.
The plugin registers TWO hooks (the classifier transform and the error
observer); both must be listed. If you've edited either file, make sure
they agree. The shipped version of this plugin has them in sync; this
warning should only appear if you're modifying the plugin.

## "I want to test it without waiting for a real Claude usage limit"

You can dry-run the scheduling half directly:

```bash
cd "$HERMES_HOME/plugins/claude-usage-autoresume"
python -c "
import sys; sys.path.insert(0, '.')
from datetime import datetime, timedelta, timezone
from schedule import schedule_resume_job
resume_at = datetime.now(timezone.utc) + timedelta(minutes=2)
print(schedule_resume_job(session_id='test-session-123', resume_at=resume_at))
"
hermes cron list   # should show the scheduled job
hermes cron remove <job_id>   # clean up afterwards
rm "$HERMES_HOME/scripts/autoresume_test-session-123.py"
rm "$HERMES_HOME/plugins/claude-usage-autoresume/state/test-session-123.json"
```

This exercises the real `hermes cron create` path without needing to
actually exhaust your usage. Run it with Hermes's interpreter for the full
plugin import:

```bash
"$LOCALAPPDATA/hermes/hermes-agent/venv/Scripts/python.exe" tests/dryrun_schedule.py
```

(Or the manual `python -c` form above from inside the plugin directory.)

## "The resume keeps rescheduling every ~55 minutes instead of resuming"

That's the watchdog hop chain doing its job: at fire time the script found
the usage window still closed (no known reset time, clock drift, or the
provider re-capped), so it re-checked 55 minutes later instead of wasting a
resume attempt. It will keep hopping until the window genuinely opens —
check `hermes cron list` for the latest hop's fire time, and the provider's
usage page for the real window. If you want it stopped entirely, see
"Retries forever" in [`limitations.md`](limitations.md).
