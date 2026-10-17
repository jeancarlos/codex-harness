import io
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from codejig import VERSION
from codejig.doctor import package_digest

TAG = re.compile(r"v\d+\.\d+\.\d+(?:-rc\d+)?")
HIDDEN = (".github", "ci", "fuzz", ".clusterfuzzlite", "SECURITY.md")
DATA = ("core", "skills", "bench", "overlay-examples")


def archive() -> bytes:
    return subprocess.run(["git", "-C", str(ROOT), "archive", "--format=zip", "--worktree-attributes", "HEAD", "--", ".",
                           *(f":(exclude){p}" for p in HIDDEN)], check=True, capture_output=True).stdout


def valid(tag: str) -> bool:
    return bool(TAG.fullmatch(tag)) and tag == f"v{VERSION}"


def stage(tag: str, root: Path) -> None:
    zipfile.ZipFile(io.BytesIO(archive())).extractall(root)
    code = root / "codejig"
    core = code / "_core"
    core.mkdir()
    for name in DATA:
        shutil.move(root / name, core / name)
    (core / "VERSION").write_text(f"{tag}\n{package_digest(code)}\n", encoding="utf-8", newline="\n")


def build(tag: str, out: Path) -> None:
    if not valid(tag):
        raise SystemExit(f"build_wheel: tag {tag} does not match code-jig VERSION {VERSION}")
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        stage(tag, root)
        subprocess.run([sys.executable, "-m", "build", "--wheel", "--no-isolation", "--outdir", str(out), str(root)],
                       check=True)


def main(argv: list[str]) -> None:
    build(argv[0] if argv else f"v{VERSION}", Path(argv[1]) if len(argv) > 1 else ROOT / "dist")


if __name__ == "__main__":
    main(sys.argv[1:])
