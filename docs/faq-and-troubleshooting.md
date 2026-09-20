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

2. **Was the failure actually classified as `rate_limit`, not `billing`?**

   This plugin deliberately ignores `billing`/`billing_unverified`
   failures (see [`how-it-works.md`](how-it-works.md#2-filtering-is-this-actually-a-claude-usage-limit-halt)
   for why). If Claude's error looked like a usage-limit message to you
   but Hermes's classifier called it `billing`, the plugin will correctly
   do nothing — that's usually a sign of something billing-related (e.g.
   an ambiguous/edge-case error body Hermes couldn't confidently call a
   pure rate limit). Check Hermes's own logs for the classified
   `failure_reason` on that turn.

3. **Was it actually the FINAL retry attempt?**

   `api_request_error` can fire multiple times per turn as Hermes retries.
   This plugin only acts on the last one (`retry_count >= max_retries`). If
   your turn ultimately succeeded on a retry, there's nothing to schedule —
   working as intended.

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

Every session where a qualifying halt (`anthropic` + `rate_limit` + final
attempt) occurred gets its own independent scheduled resume, keyed by
`session_id`. If you have several sessions halted by the same account-wide
Claude usage cap, each gets its own cron job, all resolving to roughly the
same reset time (since it's the same account/window). They will fire
independently and each resume its own conversation — see
[`limitations.md`](limitations.md) for a note on what happens if several
fire at almost the same moment.

## "Does this cost extra tokens or API calls?"

- The usage-window lookup (`fetch_account_usage`) calls Anthropic's OAuth
  usage endpoint — this is an account metadata call, not a model
  inference call, and is the same endpoint Hermes's own `/usage` command
  uses. No extra token spend.
- The cron job itself runs `--no-agent` (see
  [`how-it-works.md`](how-it-works.md#4-scheduling-the-resume-hermess-own-cron-system))
  — the scheduled job's own execution doesn't call the LLM.
- The resume it triggers (`hermes chat --resume <id>`) sends one new turn
  ("Continue where we left off.") — this is a normal Hermes turn and will
  use tokens/usage exactly like any message you'd type yourself.

## "Does it work with API keys instead of OAuth?"

No, by design. See [`limitations.md`](limitations.md) — Anthropic API key
billing doesn't have the same kind of periodic reset window that Claude
Pro/Max OAuth subscriptions do; auto-resuming an API-key billing wall would
just retry into the same failure.

## "Can I use this with other providers (OpenAI, etc.)?"

Not currently — the plugin hardcodes `provider == "anthropic"`. See
[`limitations.md`](limitations.md) for why, and how you could adapt it if
you want to extend it yourself.

## "The plugin doctor shows a WARN about provides_hooks"

```
WARN: registration adds hook 'api_request_error' not listed in provides_hooks
```

Means `plugin.yaml`'s `provides_hooks:` list doesn't match what
`register()` actually calls `ctx.register_hook()` with in `__init__.py`.
If you've edited either file, make sure they agree. The shipped version of
this plugin has them in sync; this warning should only appear if you're
modifying the plugin.

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
actually exhaust your Claude usage.
