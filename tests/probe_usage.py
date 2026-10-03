"""Read-only live probe: print each provider's usage windows and what the plugin
would pick as the resume time. Run with Hermes's interpreter:

    "$LOCALAPPDATA/hermes/hermes-agent/venv/Scripts/python.exe" tests/probe_usage.py anthropic openai-codex zai
"""
import importlib.util
import os
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(os.environ["LOCALAPPDATA"]) / "hermes" / "hermes-agent"))
_spec = importlib.util.spec_from_file_location(
    "autoresume_plugin", ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
plugin = importlib.util.module_from_spec(_spec)
sys.modules["autoresume_plugin"] = plugin
_spec.loader.exec_module(plugin)


def _fmt(epoch):
    return datetime.fromtimestamp(epoch).astimezone().isoformat(timespec="minutes") if epoch else "-"


for provider in sys.argv[1:] or ["anthropic", "openai-codex", "zai"]:
    try:
        windows = plugin.limits.fetch_windows(provider)
    except Exception as exc:  # show the real failure, unlike the cached wrapper
        print(f"{provider}: ERROR {type(exc).__name__}: {exc}")
        continue
    print(f"{provider}: {len(windows)} window(s)")
    for w in windows:
        print(f"   {w.label:16} used={w.used_percent!s:6} resets={_fmt(w.reset_epoch)}")
    print(f"   blocking reset -> {_fmt(plugin.limits.blocking_reset_epoch(windows, now=time.time()))}")
