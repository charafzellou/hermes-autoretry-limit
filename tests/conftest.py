"""Load the repo root (which IS the plugin package) as ``autoresume_plugin`` so
tests can import it under a stable name. The __init__.py dual-mode import block
(relative first, standalone fallback) makes this work even when pytest's Package
node has re-imported the file as a bare top-level module."""
import importlib.util
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent

if "autoresume_plugin" not in sys.modules:
    _spec = importlib.util.spec_from_file_location(
        "autoresume_plugin", _ROOT / "__init__.py",
        submodule_search_locations=[str(_ROOT)],
    )
    assert _spec is not None and _spec.loader is not None
    _mod = importlib.util.module_from_spec(_spec)
    sys.modules["autoresume_plugin"] = _mod
    _spec.loader.exec_module(_mod)
    import autoresume_plugin  # noqa: F401  # re-export through the package attribute
