import os
from pathlib import Path

CODE = Path(__file__).resolve().parent


def locate_core(code: Path) -> Path:
    return code / "_core" if (code / "_core").is_dir() else code.parent


CORE = locate_core(CODE)


def codex_home(arg: str | None) -> Path:
    return Path(arg or os.environ.get("CODEX_HOME") or Path.home() / ".codex").expanduser()


def state_dir() -> Path:
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "code-jig"
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "code-jig"


def default_overlay() -> Path:
    return Path.home() / "code-jig-overlay"


def overlay_dir(arg: str | None) -> Path:
    value = arg or os.environ.get("CODE_JIG_OVERLAY")
    if not value and (default_overlay() / "overlay.toml").is_file():
        value = str(default_overlay())
    if not value:
        raise SystemExit(f"code-jig: no overlay; run code-jig init-overlay, pass --overlay PATH or set CODE_JIG_OVERLAY "
                         f"(default {default_overlay()})")
    root = Path(value).expanduser().absolute()
    if not (root / "overlay.toml").is_file():
        raise SystemExit(f"code-jig: {root / 'overlay.toml'} not found")
    return root


def agents_skills() -> Path:
    return Path(os.environ.get("CODE_JIG_SKILLS_DIR") or Path.home() / ".agents" / "skills").expanduser()


def claude_code_settings() -> Path | None:
    path = Path.home() / ".claude" / "settings.json"
    return path if path.is_file() else None
