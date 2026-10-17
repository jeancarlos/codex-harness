import os
import shutil
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

from codejig import VERSION, paths

REQUIRED = ("core_version", "core_writable")


@dataclass(frozen=True)
class Overlay:
    root: Path
    core_version: str
    core_writable: bool
    auto_compact_token_limit: int | None = None
    git_gate: bool = False
    prose_gate: bool = False
    push_cleanup: bool | None = None


def _flags(path: Path, data: dict, compact) -> dict:
    if compact is not None and (isinstance(compact, bool) or not isinstance(compact, int) or compact <= 0):
        raise ValueError(f"{path}: auto_compact_token_limit must be a positive integer")
    flags = {k: data.get(k, False) for k in ("git_gate", "prose_gate")}
    flags["push_cleanup"] = data.get("push_cleanup")
    bad = [k for k, v in flags.items() if not isinstance(v, bool) and not (k == "push_cleanup" and v is None)]
    if bad:
        raise ValueError(f"{path}: {', '.join(bad)} must be true or false")
    return flags


def load(root: Path) -> Overlay:
    path = root / "overlay.toml"
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8-sig"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as e:
        raise ValueError(f"{path}: {e}") from e
    missing = [k for k in REQUIRED if k not in data]
    if missing:
        raise ValueError(f"{path}: missing {', '.join(missing)}")
    if not isinstance(data["core_writable"], bool):
        raise ValueError(f"{path}: core_writable must be true or false")
    compact = data.get("auto_compact_token_limit")
    flags = _flags(path, data, compact)
    return Overlay(root, str(data["core_version"]), data["core_writable"], compact, **flags)


def init(target: Path) -> Path:
    target = target.expanduser().absolute()
    if target.exists() and (not target.is_dir() or any(target.iterdir())):
        raise FileExistsError(f"{target} exists and is not empty")
    shutil.copytree(paths.CORE / "overlay-examples" / "personal", target, dirs_exist_ok=True)
    return target


def cli(args) -> int:
    try:
        target = init(Path(args.path) if args.path else paths.default_overlay())
    except FileExistsError as e:
        print(f"code-jig: {e}; pick an empty folder", file=sys.stderr)
        return 1
    env = f'$env:CODE_JIG_OVERLAY = "{target}"' if os.name == "nt" else f"export CODE_JIG_OVERLAY='{target}'"
    default = target == paths.default_overlay()
    print(f"code-jig: starter overlay written to {target} (core_version = v{VERSION})\n"
          "next:\n"
          f"  1. edit {target / 'profiles.toml'} to pick your models and {target / 'AGENTS.overlay.md'} for your rules\n"
          f"  2. {'code-jig finds this folder by default' if default else env}\n"
          "  3. code-jig install\n"
          "  4. code-jig doctor")
    return 0
