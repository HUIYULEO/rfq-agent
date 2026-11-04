"""Optional vendored-dependency bootstrap.

``.deps/`` is a local cache (gitignored), not a requirement: if it exists we put it
first on ``sys.path`` so the export/parse paths work in an offline demo machine;
if it does not, normal imports still apply.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEPS = ROOT / ".deps"


def ensure_deps() -> Path | None:
    if not DEPS.is_dir():
        return None
    entry = str(DEPS)
    if entry not in sys.path:
        sys.path.insert(0, entry)
    return DEPS


def imported(name: str):
    """Import a third-party module after wiring the optional cache."""
    ensure_deps()
    return __import__(name)

def require(name: str, hint: str):
    try:
        return imported(name)
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise ImportError(f"{name} is required for {hint}; run: pip install -e '.[documents]'") from exc