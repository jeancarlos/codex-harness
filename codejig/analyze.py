from __future__ import annotations

import csv
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
from collections import Counter
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path

CATEGORIES = ("security", "secret", "dependency", "bug", "type", "unused", "complexity", "duplication")
TOOL_ENV = {"SEMGREP_SEND_METRICS": "off", "SEMGREP_ENABLE_VERSION_CHECK": "0", "DOTNET_CLI_TELEMETRY_OPTOUT": "1",
            "DOTNET_NOLOGO": "1", "KIOTA_CLI_TELEMETRY_OPTOUT": "1", "PYTHONUTF8": "1"}
COGNITIVE, CYCLOMATIC, NLOC, PARAMS = 15, 15, 100, 7
CODE_SUFFIXES = {".py", ".cs", ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts", ".vue", ".java", ".kt", ".go", ".rs", ".swift", ".php", ".rb", ".scala", ".lua", ".c", ".cc", ".cpp", ".h", ".hpp", ".m"}
SKIP_DIRS = {".git", "node_modules", "bin", "obj", ".venv", "venv", "dist", "build", "__pycache__"}
ESLINT_COMPLEXITY = {"complexity", "max-depth", "max-lines-per-function", "max-params", "max-nested-callbacks",
                     "sonarjs/cognitive-complexity", "sonarjs/cyclomatic-complexity", "sonarjs/nested-control-flow"}
ESLINT_SECURITY = {f"sonarjs/{r}" for r in (
    "code-eval", "cookie-no-httponly", "cors", "csrf", "encryption-secure-mode", "file-permissions", "hardcoded-secret-signatures",
    "hashing", "insecure-cookie", "no-clear-text-protocols", "no-hardcoded-ip", "no-hardcoded-passwords", "no-hardcoded-secrets",
    "no-ip-forward", "no-os-command-from-path", "no-unsafe-unzip")}
DOTNET_COMPLEXITY = {"S3776", "S1541", "S138", "S107", "CA1502", "CA1505", "CA1506"}
DOTNET_SECURITY = {"CA2100", "S2068", "S2077", "S2245", "S3649", "S4423", "S4426", "S4433", "S4507", "S4790",
                   "S5332", "S5344", "S5443", "S5542", "S5547", "S5659", "S5773", "S6418"}
MSBUILD = re.compile(r"^(.+?)\((\d+),\d+\): (warning|error) ([A-Za-z]+\d+): (.*?)(?: \[[^\]]*\])?$")
MSBUILD_PROJECT = re.compile(r"^(.+?) : (warning|error) ([A-Za-z]+\d+): (.*?)(?: \[[^\]]*\])?$")
VULTURE = re.compile(r"^(.+?):(\d+): (.+?) \((\d+)% confidence[^)]*\)$")
KNIP_UNUSED = ("files", "dependencies", "devDependencies", "optionalPeerDependencies", "exports", "types", "nsExports", "nsTypes",
               "enumMembers", "namespaceMembers", "duplicates", "catalog", "catalogReferences")
KNIP_BROKEN = ("unlisted", "unresolved", "binaries", "cycles")
MYPY = re.compile(r"^(.+?):(\d+)(?::\d+)*: error: (.*?)(?:  \[([\w-]+)\])?$")
DUP_LOC = re.compile(r"^(.+):(\d+) ~ (\d+)$")


@dataclass(frozen=True)
class Finding:
    category: str
    tool: str
    rule: str
    path: str
    line: int
    message: str


@dataclass(frozen=True)
class Tool:
    name: str
    stack: str
    argv: Callable[[str, Path, Path], list[str]]
    parse: Callable[[str, Path, Path], list[Finding]]
    applies: Callable[[Path], bool] = lambda root: True
    hint: str = ""
    ok: tuple[int, ...] = (0,)
    reason: str = ""
    binary: str = ""
    findings_exit: tuple[int, ...] = ()


class Partial(ValueError):
    def __init__(self, message: str, findings: list[Finding]):
        super().__init__(message)
        self.findings = findings


@dataclass(frozen=True)
class Report:
    findings: list[Finding]
    skipped: list[tuple[str, str]]
    errors: list[tuple[str, str]]
    ran: list[str] = field(default_factory=list)


def detect(root: Path) -> set[str]:
    stacks = set()
    if any(root.glob("*.sln")) or any(root.glob("*.slnx")) or any(root.glob("*.csproj")) or any(root.glob("*/*.csproj")):
        stacks.add("dotnet")
    if (root / "package.json").is_file():
        stacks.add("node")
    markers = ("pyproject.toml", "setup.py", "setup.cfg")
    if any((root / m).is_file() for m in markers) or any(root.glob("requirements*.txt")) or any(root.glob("*.py")) or any(root.glob("*/*.py")):
        stacks.add("python")
    return stacks


def resolve(name: str, root: Path, stack: str, path: str | None = None) -> str | None:
    if stack == "node":
        local = shutil.which(name, path=str(root / "node_modules" / ".bin"))
        if local:
            return local
    return shutil.which(name, path=path)


def rel(path: str, root: Path) -> str:
    p = Path(path)
    if not p.is_absolute():
        p = root / p
    try:
        return p.resolve().relative_to(root.resolve()).as_posix()
    except (ValueError, OSError):
        return Path(path).as_posix()


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([shutil.which("git") or "git", *args], cwd=root, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False)


def source_files(root: Path) -> list[str]:
    try:
        r = _git(root, "ls-files", "-z", "--cached", "--others", "--exclude-standard")
    except OSError:
        r = None
    if r is not None and r.returncode == 0:
        names = [n for n in r.stdout.split("\0") if n]
    else:
        names = []
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            names += [Path(dirpath, f).relative_to(root).as_posix() for f in filenames]
    return sorted(n for n in names if (root / n).is_file())


def changed(root: Path, base: str) -> set[str]:
    mb = _git(root, "merge-base", base, "HEAD")
    if mb.returncode != 0:
        raise ValueError(f"{base}: {(mb.stderr.strip().splitlines() or ['not a commit'])[0]}")
    diff = _git(root, "diff", "--name-only", "--no-renames", "--relative", "-z", mb.stdout.strip())
    extra = _git(root, "ls-files", "--others", "--exclude-standard", "-z")
    for r in (diff, extra):
        if r.returncode != 0:
            raise ValueError(f"git cannot list the changed files: {_first(r.stderr) or f'exit {r.returncode}'}")
    return {n for n in (diff.stdout + "\0" + extra.stdout).split("\0") if n}


def _json(out: str, kind: type):
    data = json.loads(out)
    if not isinstance(data, kind):
        raise ValueError(f"expected a JSON {kind.__name__}")
    return data


def _lizard_args(exe: str, root: Path, tmp: Path) -> list[str]:
    listing = tmp / "files.txt"
    code = [n for n in source_files(root) if Path(n).suffix.lower() in CODE_SUFFIXES]
    listing.write_text("\n".join(code) + "\n", encoding="utf-8")
    return [exe, "-Ecognitive", "-Eduplicate", "--csv", "-f", str(listing)]


def _lizard_metrics(rows: list[str], root: Path) -> list[Finding]:
    found = []
    for row in csv.reader(rows):
        if len(row) < 11 or not row[0].isdigit():
            continue
        cog = int(row[11]) if len(row) > 11 and row[11].isdigit() else 0
        path, name, start = rel(row[6], root), row[7], int(row[9])
        for rule, value, limit, label in (("cognitive-complexity", cog, COGNITIVE, "cognitive complexity"),
                                          ("cyclomatic-complexity", int(row[1]), CYCLOMATIC, "cyclomatic complexity"),
                                          ("function-length", int(row[0]), NLOC, "lines of code"),
                                          ("parameters", int(row[3]), PARAMS, "parameters")):
            if value > limit:
                found.append(Finding("complexity", "lizard", rule, path, start, f"{name} has {value} {label} (limit {limit})"))
    return found


def _lizard_duplicates(lines: list[str], root: Path) -> list[Finding]:
    found = []
    block: list[tuple[str, int, int]] = []
    for line in lines:
        m = DUP_LOC.match(line.strip())
        if m:
            block.append((rel(m.group(1), root), int(m.group(2)), int(m.group(3))))
        elif line.startswith("^^^") and block:
            (path, start, _), others = block[0], block[1:]
            where = ", ".join(f"{p}:{s}-{e}" for p, s, e in others)
            found.append(Finding("duplication", "lizard", "duplicate-block", path, start, f"same code as {where}"))
            block = []
    return found


def parse_lizard(out: str, root: Path, tmp: Path) -> list[Finding]:
    lines = out.splitlines()
    split = lines.index("Duplicates") if "Duplicates" in lines else len(lines)
    return _lizard_metrics(lines[:split], root) + _lizard_duplicates(lines[split:], root)


def parse_ruff(out: str, root: Path, tmp: Path) -> list[Finding]:
    found = []
    for item in _json(out, list):
        code = item.get("code")
        if not code:
            category, rule = "bug", "syntax"
        elif re.fullmatch(r"S\d+", code):
            category, rule = "security", code
        elif code.startswith(("C90", "PLR09")):
            category, rule = "complexity", code
        else:
            category, rule = "bug", code
        found.append(Finding(category, "ruff", rule, rel(item["filename"], root), int(item["location"]["row"]), item["message"]))
    return found


def parse_mypy(out: str, root: Path, tmp: Path) -> list[Finding]:
    return [Finding("type", "mypy", m.group(4) or "error", rel(m.group(1), root), int(m.group(2)), m.group(3))
            for m in map(MYPY.match, out.splitlines()) if m]


def parse_eslint(out: str, root: Path, tmp: Path) -> list[Finding]:
    found = []
    for result in _json(out, list):
        path = rel(result["filePath"], root)
        for msg in result.get("messages") or []:
            rule = msg.get("ruleId") or "parse"
            if rule in ESLINT_COMPLEXITY:
                category = "complexity"
            elif rule.startswith("security/") or rule in ESLINT_SECURITY:
                category = "security"
            else:
                category = "bug"
            found.append(Finding(category, "eslint", rule, path, int(msg.get("line") or 0), msg.get("message", "")))
    return found


def parse_tsc(out: str, root: Path, tmp: Path) -> list[Finding]:
    return [Finding("type", "tsc", m.group(4), rel(m.group(1), root), int(m.group(2)), m.group(5))
            for m in map(MSBUILD.match, out.splitlines()) if m]


def _dotnet_category(rule: str) -> str:
    if re.fullmatch(r"NU190[1-4]", rule):
        return "dependency"
    if rule in DOTNET_COMPLEXITY:
        return "complexity"
    if rule in DOTNET_SECURITY or rule.startswith(("CA3", "CA5", "CA23")):
        return "security"
    if rule.startswith("CS"):
        return "type"
    return "bug"


def _dotnet_lines(out: str):
    for line in (ln.strip() for ln in out.splitlines()):
        m = MSBUILD.match(line)
        if m:
            yield m.group(4), m.group(1), int(m.group(2)), m.group(5)
            continue
        m = MSBUILD_PROJECT.match(line)
        if m:
            yield m.group(3), m.group(1), 0, m.group(4)


def parse_dotnet(out: str, root: Path, tmp: Path) -> list[Finding]:
    seen: dict[tuple, Finding] = {}
    for rule, path, line, message in _dotnet_lines(out):
        f = Finding(_dotnet_category(rule), "dotnet", rule, rel(path, root), line, message)
        seen.setdefault((f.path, f.line, f.rule, f.message), f)
    return list(seen.values())


def parse_vulture(out: str, root: Path, tmp: Path) -> list[Finding]:
    found = []
    for m in map(VULTURE.match, out.splitlines()):
        if m:
            rule = re.sub(r"\W+", "-", re.sub(r"'[^']*'", " ", m.group(3)).strip().lower()).strip("-")
            found.append(Finding("unused", "vulture", rule, rel(m.group(1), root), int(m.group(2)), f"{m.group(3)} ({m.group(4)}% confidence)"))
    return found


def _knip_item(entry: dict, key: str, item, root: Path) -> Finding:
    category = "bug" if key in KNIP_BROKEN else "unused"
    if isinstance(item, list):
        names = " -> ".join(str(i.get("name", "")) for i in item)
        return Finding(category, "knip", f"knip/{key}", rel(entry["file"], root), 0, f"{key}: {names}")
    name = ".".join(str(part) for part in (item.get("namespace"), item.get("name", "")) if part)
    return Finding(category, "knip", f"knip/{key}", rel(entry["file"], root), int(item.get("line") or 0), f"{key}: {name}")


def parse_knip(out: str, root: Path, tmp: Path) -> list[Finding]:
    issues = _json(out, dict).get("issues")
    if not isinstance(issues, list):
        raise ValueError("expected an issues list")
    return [_knip_item(entry, key, item, root)
            for entry in issues for key in KNIP_UNUSED + KNIP_BROKEN for item in entry.get(key) or []]


def parse_semgrep(out: str, root: Path, tmp: Path) -> list[Finding]:
    data = _json(out, dict)
    found = [Finding("security", "semgrep", r["check_id"], rel(r["path"], root), int(r["start"]["line"]), r["extra"].get("message", ""))
             for r in data.get("results") or []]
    errors = data.get("errors") or []
    if errors:
        first = errors[0] if isinstance(errors[0], dict) else {"message": str(errors[0])}
        raise Partial(f"{len(errors)} errors, first: {first.get('type', 'error')}: {first.get('message', '')}".strip(), found)
    return found


def parse_gitleaks(out: str, root: Path, tmp: Path) -> list[Finding]:
    report = tmp / "gitleaks.json"
    if not report.is_file() or not report.read_text(encoding="utf-8").strip():
        return []
    return [Finding("secret", "gitleaks", r.get("RuleID", "secret"), rel(r["File"], root), int(r.get("StartLine") or 0),
                    r.get("Description", "") + (f" (fingerprint {r['Fingerprint']})" if r.get("Fingerprint") else ""))
            for r in _json(report.read_text(encoding="utf-8"), list)]


def parse_osv(out: str, root: Path, tmp: Path) -> list[Finding]:
    if not out.strip():
        return []
    found = []
    for result in _json(out, dict).get("results") or []:
        path = rel(result["source"]["path"], root)
        for pkg in result.get("packages") or []:
            name = f"{pkg['package']['name']}@{pkg['package'].get('version', '?')}"
            for vuln in pkg.get("vulnerabilities") or []:
                found.append(Finding("dependency", "osv-scanner", vuln["id"], path, 0, f"{name}: {vuln.get('summary', '')}"))
    return found


def _has_mypy_config(root: Path) -> bool:
    def contains(name: str, text: str) -> bool:
        f = root / name
        return f.is_file() and text in f.read_text(encoding="utf-8", errors="replace")
    return (root / "mypy.ini").is_file() or (root / ".mypy.ini").is_file() or contains("setup.cfg", "[mypy") or contains("pyproject.toml", "[tool.mypy")


def _vulture_args(exe: str, root: Path, tmp: Path) -> list[str]:
    pyproject = root / "pyproject.toml"
    if pyproject.is_file() and "[tool.vulture" in pyproject.read_text(encoding="utf-8", errors="replace"):
        return [exe]
    excludes = ",".join(f"{d}/*,*/{d}/*" for d in sorted(SKIP_DIRS))
    return [exe, ".", "--min-confidence", "80", "--exclude", excludes]


def _has_eslint_config(root: Path) -> bool:
    return any(root.glob("eslint.config.*")) or any(root.glob(".eslintrc*"))


def _semgrep_rules(root: Path) -> Path | None:
    for name in (".semgrep.yml", ".semgrep.yaml", ".semgrep"):
        if (root / name).exists():
            return root / name
    return None


def _dotnet_target(root: Path) -> list[str]:
    for pattern in ("*.sln", "*.slnx", "*.csproj"):
        found = sorted(root.glob(pattern))
        if found:
            return [found[0].name]
    return []


_GITLEAKS = ("--no-banner", "--redact", "--exit-code", "0", "--report-format", "json", "--report-path")

TOOLS = [
    Tool("lizard", "any", _lizard_args, parse_lizard, hint="pip install lizard"),
    Tool("ruff", "python", lambda exe, root, tmp: [exe, "check", "--output-format", "json", "--no-cache", "--exit-zero", "--extend-select", "S,B", "."],
         parse_ruff, hint="pip install ruff"),
    Tool("vulture", "python", _vulture_args,
         parse_vulture, hint="pip install vulture", ok=(0, 3)),
    Tool("mypy", "python", lambda exe, root, tmp: [exe, "--no-error-summary", "--no-pretty", "--show-error-codes", "."],
         parse_mypy, _has_mypy_config, "pip install mypy", reason="no mypy config", findings_exit=(1,)),
    Tool("eslint", "node", lambda exe, root, tmp: [exe, ".", "-f", "json"], parse_eslint, _has_eslint_config,
         "npm install -D eslint eslint-plugin-sonarjs", reason="no eslint config", findings_exit=(1,)),
    Tool("knip", "node", lambda exe, root, tmp: [exe, "--reporter", "json", "--no-progress"], parse_knip,
         hint="npm install -D knip", findings_exit=(1,)),
    Tool("tsc", "node", lambda exe, root, tmp: [exe, "--noEmit", "--pretty", "false", "-p", "."], parse_tsc,
         lambda root: (root / "tsconfig.json").is_file(), "npm install -D typescript", reason="no tsconfig.json", findings_exit=(1, 2)),
    Tool("dotnet", "dotnet", lambda exe, root, tmp: [exe, "build", "--nologo", "--no-incremental", "-clp:NoSummary", *_dotnet_target(root)],
         parse_dotnet, lambda root: bool(_dotnet_target(root)), "install the .NET SDK", reason="no solution or project at the root", findings_exit=(1,)),
    Tool("semgrep", "any", lambda exe, root, tmp: [exe, "scan", "--metrics=off", "--disable-version-check", "--config", str(_semgrep_rules(root)), "--json", "--quiet", "."],
         parse_semgrep, lambda root: _semgrep_rules(root) is not None, "pip install semgrep", reason="no local rules (.semgrep.yml or .semgrep/)"),
    Tool("gitleaks", "any", lambda exe, root, tmp: [exe, "dir", *_GITLEAKS, str(tmp / "gitleaks.json"), "."],
         parse_gitleaks, hint="install gitleaks from its GitHub releases", binary="gitleaks"),
    Tool("gitleaks-history", "any", lambda exe, root, tmp: [exe, "git", *_GITLEAKS, str(tmp / "gitleaks.json"), "."],
         parse_gitleaks, lambda root: (root / ".git").exists(), "install gitleaks from its GitHub releases", reason="not a git repository", binary="gitleaks"),
    Tool("osv-scanner", "any", lambda exe, root, tmp: [exe, "scan", "source", "-r", "--offline", "--download-offline-databases", "--format", "json", "."],
         parse_osv, hint="install osv-scanner from its GitHub releases", ok=(0, 1, 128)),
]


def _first(text: str) -> str:
    return (text.strip().splitlines() or [""])[0][:300]


def _kill_tree(proc: subprocess.Popen) -> None:
    if os.name == "nt":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True, check=False)
        return
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except OSError:
        proc.kill()


def _execute(argv: list[str], root: Path, env: dict, timeout: int) -> tuple[int, str, str]:
    group = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
    with subprocess.Popen(argv, cwd=root, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          text=True, encoding="utf-8", errors="replace", **group) as proc:
        try:
            out, err = proc.communicate(timeout=timeout)
        except BaseException:
            _kill_tree(proc)
            proc.communicate()
            raise
    return proc.returncode, out, err


def _run_one(t: Tool, exe: str, root: Path, env: dict, timeout: int) -> tuple[list[Finding], str | None]:
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        try:
            code, out, err = _execute(t.argv(exe, root, tmp), root, env, timeout)
        except subprocess.TimeoutExpired:
            return [], f"timed out after {timeout}s"
        except OSError as e:
            return [], f"cannot run: {e}"
        error = None
        try:
            found = t.parse(out, root, tmp)
        except Partial as e:
            found, error = e.findings, str(e)
        except (ValueError, KeyError, TypeError, IndexError, AttributeError) as e:
            return [], f"unreadable output: {e}; {_first(err)}"
    if code not in t.ok and (code not in t.findings_exit or not found):
        error = f"exit {code}: {_first(err) or _first(out)}"
    return found, error


def _add_new(findings: list[Finding], seen: set, found: list[Finding]) -> None:
    for f in found:
        key = (f.tool, f.rule, f.path, f.line, "" if f.line else f.message)
        if key not in seen:
            seen.add(key)
            findings.append(f)


def run_tools(tools: list[Tool], root: Path, stacks: set[str], path: str | None = None, timeout: int = 600) -> Report:
    report = Report([], [], [])
    env = {**os.environ, **TOOL_ENV}
    if path is not None:
        env["PATH"] = path
    seen = set()
    for t in tools:
        if not stacks or (t.stack != "any" and t.stack not in stacks):
            continue
        if not t.applies(root):
            report.skipped.append((t.name, t.reason))
            continue
        exe = resolve(t.binary or t.name, root, t.stack, path)
        if exe is None:
            report.skipped.append((t.name, f"not installed; {t.hint}"))
            continue
        found, error = _run_one(t, exe, root, env, timeout)
        report.ran.append(t.name)
        _add_new(report.findings, seen, found)
        if error:
            report.errors.append((t.name, error))
    return report


def printable(text: str) -> str:
    return "".join(ch if ch.isprintable() else f"\\x{ord(ch):02x}" for ch in text)


def render(report: Report, stacks: set[str], top: int = 10) -> list[str]:
    counts = Counter(f.category for f in report.findings)
    out = [f"analyze: {', '.join(sorted(stacks))}", "  ".join(f"{c} {counts.get(c, 0)}" for c in CATEGORIES)]
    for category in CATEGORIES:
        items = sorted((f for f in report.findings if f.category == category), key=lambda f: (f.path, f.line, f.rule))
        if not items:
            continue
        out.append(f"== {category} ({len(items)})")
        for f in items[:top]:
            where = f"{f.path}:{f.line}" if f.line else f.path
            out.append(f"  {printable(where)}  {printable(f.rule)}  {printable(f.message)[:200]}")
        if len(items) > top:
            out.append(f"  and {len(items) - top} more")
    out += [f"skipped: {name} ({why})" for name, why in report.skipped]
    out += [f"error: {name}: {printable(why)}" for name, why in report.errors]
    return out


def utf8_stdout() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")


def cli(args) -> int:
    utf8_stdout()
    root = Path(args.path).resolve()
    stacks = detect(root)
    if not stacks:
        print(f"code-jig: no .NET, Node or Python project found in {root}", file=sys.stderr)
        return 2
    try:
        keep = changed(root, args.base) if args.base else None
    except (ValueError, OSError) as e:
        print(f"code-jig: {e}", file=sys.stderr)
        return 2
    report = run_tools(TOOLS, root, stacks, timeout=args.timeout)
    if keep is not None:
        report = Report([f for f in report.findings if f.path in keep], report.skipped, report.errors, report.ran)
    if args.json:
        print(json.dumps({"stacks": sorted(stacks), "findings": [asdict(f) for f in report.findings],
                          "skipped": report.skipped, "errors": report.errors}, ensure_ascii=False, indent=1))
    else:
        for line in render(report, stacks, args.top):
            print(line)
    if not report.ran:
        print("code-jig: no analyzer ran; install at least one of the skipped tools", file=sys.stderr)
        return 2
    return 1 if report.findings or report.errors else 0
