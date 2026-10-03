from datetime import datetime, timezone

from autoresume_plugin import schedule

RESET = datetime(2026, 10, 4, 3, 0, tzinfo=timezone.utc)


def test_build_cron_command_shape():
    cmd = schedule.build_cron_command(session_id="20261003_abc", resume_at=RESET,
                                      script_name="autoresume_20261003_abc.py")
    assert cmd[:4] == ["hermes", "cron", "create", "2026-10-04T03:01:00+00:00"]
    assert cmd[cmd.index("--script") + 1] == "autoresume_20261003_abc.py"
    assert "--no-agent" in cmd and cmd[cmd.index("--repeat") + 1] == "1"
    assert cmd[cmd.index("--failure-deliver") + 1] == "local"


def test_job_name_unique_per_fire_time():
    a = schedule.build_cron_command(session_id="s", resume_at=RESET, script_name="x.py")
    b = schedule.build_cron_command(session_id="s", resume_at=RESET.replace(hour=8), script_name="x.py")
    assert a[a.index("--name") + 1] != b[b.index("--name") + 1]


def test_interpreter_passed_when_known():
    cmd = schedule.build_cron_command(session_id="s", resume_at=RESET, script_name="x.py",
                                      interpreter="C:/py/python.exe")
    assert cmd[cmd.index("--interpreter") + 1] == "C:/py/python.exe"


def test_no_interpreter_flag_when_unknown():
    cmd = schedule.build_cron_command(session_id="s", resume_at=RESET, script_name="x.py")
    assert "--interpreter" not in cmd


def test_generated_script_compiles_and_hops(tmp_path, monkeypatch):
    """The generated resume script must be valid Python and must re-schedule a
    +55min check instead of resuming when the window is still closed."""
    import ast

    captured = {}
    monkeypatch.setattr(schedule, "_SCRIPTS_DIR", tmp_path)
    written = schedule._write_resume_script("sess hop", "zai")
    src = written.read_text(encoding="utf-8")
    ast.parse(src)  # syntax check — a broken template crashes cron at fire time
    assert "HOP_MINUTES = 55" in src
    assert "PROVIDER = 'zai'" in src
    assert "_schedule_next_hop" in src and '"hermes", "cron", "create"' in src
    assert "--no-agent" in src and '"--resume"' in src
    captured["path"] = written
