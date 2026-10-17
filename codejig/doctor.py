from __future__ import annotations

import hashlib
import json
import mmap
import os
import shutil
import subprocess
import tomllib
from pathlib import Path

from codejig import managed, paths, probe, tiers
from codejig.install import (
    HOOK_STATUS,
    ROLE_TIERS,
    _git_config,
    _same_path,
    render_agents,
    render_profile,
    role_files,
    skill_sources,
    valid_hook_groups,
)
from codejig.overlay import Overlay, load

Check = tuple[str, str, str]

PACK_KEYS = (
    "project_doc_max_bytes",
    "approvals_reviewer",
    "model_auto_compact_token_limit",
    "tool_output_token_limit",
    "model_reasoning_effort",
    "extra_policy",
    "allowed_approvals_reviewers",
    "ignore_rules",
    "allow_managed_hooks_only",
    "guardian_extra_policy",
    "developer_instructions",
)
SHIM_MAX_BYTES = 1_000_000


def resolve_codex_bin(arg: str | None) -> Path | None:
    value = arg or os.environ.get("CODEX_BIN") or shutil.which("codex")
    return Path(value) if value else None


def check_binary(bin_path: Path | None, keys=PACK_KEYS) -> list[Check]:
    name = "codex keys"
    if bin_path is None or not bin_path.is_file():
        return [("FAIL", name, "codex binary not found; pass --codex-bin PATH or set CODEX_BIN")]
    try:
        if bin_path.stat().st_size < SHIM_MAX_BYTES:
            return [("FAIL", name, f"{bin_path} is a launcher, not the native binary; pass --codex-bin PATH")]
        with open(bin_path, "rb") as f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as m:
            missing = [k for k in keys if m.find(k.encode()) == -1]
    except OSError as e:
        return [("FAIL", name, f"cannot read {bin_path}: {e.strerror or e}")]
    if missing:
        return [("FAIL", name, f"not in {bin_path.name}: {', '.join(missing)}")]
    return [("ok", name, f"{len(keys)} keys present in {bin_path.name}")]


def report(checks: list[Check]) -> int:
    for level, name, detail in checks:
        print(f"{level} {name}: {detail}")
    return 1 if any(c[0] == "FAIL" for c in checks) else 0


DEFAULT_DOC_MAX = 32768


def _pick(directory: Path) -> Path | None:
    for name in ("AGENTS.override.md", "AGENTS.md"):
        f = directory / name
        if f.is_file() and f.stat().st_size > 0:
            return f
    return None


def _git_root(cwd: Path) -> Path:
    for d in (cwd, *cwd.parents):
        if (d / ".git").exists():
            return d
    return cwd


def chain_files(home: Path, cwd: Path) -> list[Path]:
    files = [f for f in [_pick(home)] if f]
    root = _git_root(cwd)
    dirs = [root, *[d for d in reversed(cwd.parents) if root in d.parents], cwd] if cwd != root else [root]
    seen = set()
    for d in dirs:
        if d in seen:
            continue
        seen.add(d)
        f = _pick(d)
        if f:
            files.append(f)
    return files


def read_config(home: Path) -> dict:
    try:
        return tomllib.loads((home / "config.toml").read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}


def _doc_max(home: Path) -> int:
    value = read_config(home).get("project_doc_max_bytes", DEFAULT_DOC_MAX)
    if isinstance(value, bool) or not isinstance(value, int):
        return DEFAULT_DOC_MAX
    return value


def check_chain(home: Path, cwd: Path) -> list[Check]:
    cap = _doc_max(home)
    total = sum(f.stat().st_size for f in chain_files(home, cwd))
    detail = f"{total} of {cap} bytes"
    if total > cap:
        return [("FAIL", "AGENTS.md chain", detail + "; Codex silently stops adding files past the cap")]
    if total > cap * 0.8:
        return [("warn", "AGENTS.md chain", detail)]
    return [("ok", "AGENTS.md chain", detail)]


def check_global(home: Path, core: Path, ov: Overlay) -> list[Check]:
    name = "global AGENTS.md"
    override = home / "AGENTS.override.md"
    if override.is_file() and override.stat().st_size > 0:
        return [("FAIL", name, f"{override} is non-empty; Codex loads it instead of the generated AGENTS.md")]
    target = home / "AGENTS.md"
    if not target.is_file():
        return [("warn", name, f"{target} is missing; run code-jig install")]
    if target.read_bytes() != render_agents(core, ov.root).encode("utf-8"):
        return [("warn", name, f"{target} is stale; run code-jig install")]
    return [("ok", name, f"{target} is current")]


def catalog(home: Path) -> dict[str, set[str]] | None:
    try:
        data = json.loads((home / "models_cache.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    models = data.get("models") if isinstance(data, dict) else None
    if not isinstance(models, list):
        return None
    out = {}
    for m in models:
        if isinstance(m, dict) and isinstance(m.get("slug"), str):
            levels = m.get("supported_reasoning_levels")
            out[m["slug"]] = {lv["effort"] for lv in levels if isinstance(lv, dict) and isinstance(lv.get("effort"), str)} if isinstance(levels, list) else set()
    return out


def family(slug: str) -> str:
    return slug.rsplit("-", 1)[0]


def _legacy_check(home: Path, ts, cfg: dict) -> Check:
    profiles = cfg.get("profiles") if isinstance(cfg.get("profiles"), dict) else {}
    legacy = [t.name for t in ts if cfg.get("profile") == t.name or t.name in profiles]
    return ("FAIL", "legacy profiles", f"{home / 'config.toml'} has profile = or [profiles.*] for {', '.join(legacy)}; Codex refuses --profile with them") \
        if legacy else ("ok", "legacy profiles", "none")


def _profile_check(home: Path, t, policy, ov: Overlay) -> Check:
    f = home / f"{t.name}.config.toml"
    name = f"tier {t.name} profile"
    if not f.is_file():
        return ("warn", name, f"{f} is missing; Codex silently uses the base config; run code-jig install")
    if f.read_bytes() != render_profile(t, policy, ov.auto_compact_token_limit).encode("utf-8"):
        return ("warn", name, f"{f} is stale or edited; run code-jig install")
    return ("ok", name, f"{t.model} at {t.effort}")


def _model_check(t, cat: dict) -> Check:
    name = f"tier {t.name} model"
    if t.model not in cat:
        return ("FAIL", name, f"{t.model} is not in this account's catalog; candidates: {', '.join(sorted(cat))}")
    if cat[t.model] and t.effort not in cat[t.model]:
        return ("FAIL", name, f"{t.model} does not support {t.effort}; supported: {', '.join(sorted(cat[t.model]))}")
    return ("ok", name, f"{t.model} supports {t.effort}")


def _family_checks(ts) -> list[Check]:
    by = {t.name: t for t in ts}
    if "review" not in by or "default" not in by:
        return []
    same = family(by["review"].model) == family(by["default"].model)
    return [("warn", "review family", f"review and default are both {family(by['default'].model)}; cross-family review catches different bugs")
            if same else ("ok", "review family", f"{by['review'].model} vs {by['default'].model}")]


def check_tiers(home: Path, core: Path, ov: Overlay) -> list[Check]:
    try:
        ts = tiers.load(core, ov.root)
    except ValueError as e:
        return [("FAIL", "tiers", str(e))]
    checks: list[Check] = [_legacy_check(home, ts, read_config(home))]
    policy = tiers.extra_policy(ov.root)
    checks += [_profile_check(home, t, policy, ov) for t in ts]
    cat = catalog(home)
    if cat is None:
        checks.append(("warn", "model catalog", f"{home / 'models_cache.json'} missing or unreadable; start codex once"))
    else:
        checks += [_model_check(t, cat) for t in ts]
    return checks + _family_checks(ts)


def check_roles(home: Path, core: Path, ov: Overlay) -> list[Check]:
    try:
        ts = tiers.load(core, ov.root)
    except ValueError:
        return []
    by = {t.name: t for t in ts}
    checks: list[Check] = []
    for name, text in role_files(core, ts).items():
        role = name.removesuffix(".toml")
        t = by[ROLE_TIERS[role]]
        f = home / "agents" / name
        if not f.is_file():
            checks.append(("warn", f"{role} role", f"{f} is missing; subagents of that kind use the parent's model; run code-jig install"))
        elif f.read_bytes() != text.encode("utf-8"):
            checks.append(("warn", f"{role} role", f"{f} is stale or edited; run code-jig install"))
        elif "default" in by and t.model == by["default"].model and t.effort == by["default"].effort:
            checks.append(("warn", f"{role} role", f"{t.name} tier is the same as default ({t.model} at {t.effort}); the role saves nothing"))
        else:
            checks.append(("ok", f"{role} role", f"{role}s run {t.model} at {t.effort}"))
    return checks


def check_git_gate(ov: Overlay, cwd: Path) -> list[Check]:
    target = paths.state_dir() / "git-hooks"
    current = _git_config("--get", "core.hooksPath") or ""
    if not ov.git_gate:
        return [("warn", "git gate", f"off in overlay.toml but core.hooksPath still points at {target}; run code-jig install")
                if _same_path(current, target) else ("ok", "git gate", "off")]
    if not _same_path(current, target):
        return [("warn", "git gate", f"not active: global core.hooksPath is {current or 'unset'}, expected {target}")]
    local = subprocess.run(["git", "-C", str(cwd), "config", "--local", "--get", "core.hooksPath"],
                           capture_output=True, text=True, encoding="utf-8", check=False).stdout.strip()
    if local:
        return [("warn", "git gate", f"this repo sets core.hooksPath = {local}, which bypasses the gate here")]
    return [("ok", "git gate", f"active{' with the prose gate' if ov.prose_gate else ''}")]


def check_hook(home: Path) -> list[Check]:
    path = home / "hooks.json"
    if not path.is_file():
        return [("warn", "stop hook", f"{path} missing; run code-jig install")]
    try:
        groups = json.loads(path.read_text(encoding="utf-8-sig"))["hooks"]["Stop"]
        if not valid_hook_groups(groups):
            raise ValueError('expected {"hooks": {"Stop": [{"hooks": [...]}]}}')
    except (OSError, ValueError, KeyError, TypeError) as e:
        return [("FAIL", "stop hook", f"{path}: {e}")]
    found = isinstance(groups, list) and any(
        isinstance(g, dict) and any(isinstance(h, dict) and h.get("statusMessage") == HOOK_STATUS for h in g.get("hooks") or [])
        for g in groups)
    return [("ok", "stop hook", "registered; Codex asks to trust it once") if found
            else ("warn", "stop hook", "not registered; run code-jig install")]


def check_knowledge(ov: Overlay) -> list[Check]:
    kb = ov.root / "knowledge"
    index = kb / "INDEX.md"
    if not kb.is_dir():
        return [("ok", "knowledge", "no knowledge base")]
    if not index.is_file():
        return [("warn", "knowledge", f"{kb} has no INDEX.md; Codex is not pointed at it")]
    n = len(index.read_text(encoding="utf-8-sig").splitlines())
    return [("ok", "knowledge", f"index has {n} of 150 lines") if n <= 150
            else ("warn", "knowledge", f"{index} has {n} lines; merge or delete facts to stay at 150")]


def check_superpowers(home: Path) -> list[Check]:
    plugins = read_config(home).get("plugins")
    if isinstance(plugins, dict) and any(k.startswith("superpowers@") for k in plugins) \
            or any((home / "plugins" / "cache").glob("*/superpowers")):
        return [("ok", "superpowers", "installed")]
    return [("warn", "superpowers", "not installed; run: codex plugin add superpowers@openai-curated-remote")]


def _git(core: Path, *args: str) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(["git", "-C", str(core), *args], capture_output=True, text=True, encoding="utf-8", check=False)
    except OSError:
        return None


def package_digest(code: Path) -> str:
    h = hashlib.sha256()
    files = []
    for p in code.rglob("*"):
        rel = p.relative_to(code)
        if p.is_file() and "__pycache__" not in rel.parts and rel.as_posix() != "_core/VERSION":
            files.append((rel.as_posix().encode(), p))
    for rel, p in sorted(files):
        data = p.read_bytes()
        h.update(len(rel).to_bytes(8, "big") + rel + len(data).to_bytes(8, "big") + data)
    return h.hexdigest()


def check_release(core: Path, ov: Overlay) -> list[Check]:
    strict = "FAIL" if not ov.core_writable else "warn"
    try:
        words = (core / "VERSION").read_text(encoding="utf-8", errors="replace").split()
    except OSError as e:
        return [("FAIL", "core pin", f"cannot read VERSION: {e}")]
    tag, digest = (words + ["", ""])[:2]
    pin = ("ok", "core pin", tag) if tag == ov.core_version else (
        strict, "core pin", f"core is release {tag or 'unknown'}, overlay pins {ov.core_version}")
    if ov.core_writable:
        return [pin, ("ok", "core clean", "release core; local edits allowed")]
    if digest and digest == package_digest(core.parent):
        return [pin, ("ok", "core clean", "matches its release digest")]
    return [pin, ("FAIL", "core clean", f"{core.parent} differs from its release digest but core_writable = false")]


def _git_failure(core: Path, ov: Overlay, strict: str, status) -> list[Check]:
    reason = f"{core} is not a readable git repo or git is missing"
    first = (status.stderr.strip().splitlines() or [""])[0] if status else ""
    if first:
        reason += f": {first}"
    pin = (strict, "core pin", f"cannot be checked; overlay pins {ov.core_version}")
    return [pin, ("FAIL", "core clean", reason)]


def check_core(core: Path, ov: Overlay) -> list[Check]:
    if (core / "VERSION").is_file() and not (core / ".git").exists():
        return check_release(core, ov)
    strict = "FAIL" if not ov.core_writable else "warn"
    described = _git(core, "describe", "--tags", "--exact-match", "HEAD")
    tag = described.stdout.strip() if described and described.returncode == 0 else ""
    status = _git(core, "status", "--porcelain")
    if status is None or status.returncode != 0:
        return _git_failure(core, ov, strict, status)
    pin = ("ok", "core pin", tag) if tag == ov.core_version else (
        strict, "core pin", f"core is at {tag or 'an untagged commit'}, overlay pins {ov.core_version}")
    dirty = status.stdout.strip()
    clean = ("FAIL", "core clean", "core has local edits but core_writable = false") if dirty and not ov.core_writable \
        else ("ok", "core clean", "local edits allowed" if dirty else "no local edits")
    return [pin, clean]


def cli(args) -> int:
    bin_path = resolve_codex_bin(args.codex_bin)
    home = paths.codex_home(args.codex_home)
    checks = [("FAIL", "requirements", "git not found")] if not shutil.which("git") else []
    checks += check_binary(bin_path)
    if not args.check_binary:
        try:
            ov = load(paths.overlay_dir(args.overlay))
        except ValueError as e:
            checks.append(("FAIL", "overlay", str(e)))
        else:
            checks += check_core(paths.CORE, ov)
            checks += check_global(home, paths.CORE, ov)
            checks += check_tiers(home, paths.CORE, ov)
            checks += check_roles(home, paths.CORE, ov)
            checks += check_hook(home)
            checks += check_git_gate(ov, Path.cwd())
            checks += check_knowledge(ov)
            checks += check_superpowers(home)
            try:
                ts = tiers.load(paths.CORE, ov.root)
            except ValueError:
                ts = []
            checks += managed.check_requirements(managed.requirements_path(), paths.CORE / "bench" / "requirements.toml", ts)
            try:
                skills = sorted(skill_sources(paths.CORE, ov.root))
            except ValueError:
                skills = []
            checks += probe.check_prompt(bin_path, home, Path.cwd(), ts, skills)
            checks += probe.check_rules(bin_path, home, Path.cwd())
            if args.live:
                checks += probe.check_live(bin_path, home, Path.cwd(), ts)
        checks += check_chain(home, Path.cwd())
    return report(checks)
