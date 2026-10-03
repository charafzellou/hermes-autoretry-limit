"""One-shot dry-run: schedule a real `hermes cron` resume job 6 hours out using
the INSTALLED plugin, so the full write path (script -> hermes cron create) is
exercised. Run with Hermes's interpreter:

    "$LOCALAPPDATA/hermes/hermes-agent/venv/Scripts/python.exe" tests/dryrun_schedule.py

Clean up afterwards:
    hermes cron list            # note the job id printed below
    hermes cron remove <job_id>
    rm "$LOCALAPPDATA/hermes/scripts/autoresume_dryrun_000.py"
"""
import importlib.util
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(os.environ["LOCALAPPDATA"]) / "hermes" / "hermes-agent"))
root = Path(os.environ["LOCALAPPDATA"]) / "hermes" / "plugins" / "hermes-autoretry-limit"
spec = importlib.util.spec_from_file_location("p", root / "__init__.py",
                                              submodule_search_locations=[str(root)])
m = importlib.util.module_from_spec(spec)
sys.modules["p"] = m
spec.loader.exec_module(m)
job = m.schedule_resume_job(session_id="dryrun_000", provider="zai",
                            resume_at=datetime.now(timezone.utc) + timedelta(hours=6))
print("scheduled job:", job)
