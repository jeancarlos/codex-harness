import codecs
import json
import re
import statistics
import subprocess
import sys
import tempfile
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path

from codejig import analyze, collect, paths

TEST_MARKERS = ("pytest", "unittest", "dotnet test", "npm test", "pnpm test", "yarn test", "run test", "vitest", "jest")
GRACE = timedelta(hours=2)
REWORK_DAYS = 14
MIN_COMMITS = 20
CAVEAT = "This is correlation, not causation: session work and baseline work are different tasks."
HUNK = re.compile(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
SESSIONS = """
SELECT t.id, t.cwd, t.started, MAX(x.ts) FROM threads t
LEFT JOIN (
  SELECT thread_id AS owner, ts FROM usage
  UNION ALL SELECT thread_id, ts FROM calls
  UNION ALL SELECT c.parent_id, u.ts FROM usage u JOIN threads c ON c.id = u.thread_id WHERE c.kind != 'main'
) x ON x.owner = t.id
WHERE t.kind = 'main' AND t.started >= ?
GROUP BY t.id
"""
RECORD = "\x00\x01"
LOG_FORMAT = "--format=%x00%x01%H%x00%aI%x00%s%x00%b%x00"


@dataclass(frozen=True)
class Commit:
    sha: str
    when: datetime
    subject: str
    body: str
    hunks: dict[str, list[tuple[int, int, int, int]]] = field(default_factory=dict)


def is_test(cmd: str) -> bool:
    return any(m in cmd for m in TEST_MARKERS)


def _unquote(name: str) -> str:
    if len(name) > 1 and name[0] == name[-1] == '"':
        return codecs.escape_decode(name[1:-1].encode("utf-8"))[0].decode("utf-8", "replace")
    return name


def _file(line: str, old: str | None) -> tuple[str | None, str | None]:
    raw = _unquote(line[4:].rstrip("\t"))
    name = raw[2:] if raw[:2] in ("a/", "b/") else None
    return (name, None) if line.startswith("---") else (old, name or old)


def _add_hunk(hunks: dict, path: str | None, line: str) -> None:
    m = HUNK.match(line)
    if m and path:
        a, b, n, d = m.groups()
        hunks.setdefault(path, []).append((int(a), int(b or 1), int(n), int(d or 1)))


def _hunks(diff: str) -> dict[str, list[tuple[int, int, int, int]]]:
    hunks: dict[str, list[tuple[int, int, int, int]]] = {}
    old = path = None
    header = False
    for line in diff.splitlines():
        if line.startswith("diff --git"):
            old = path = None
            header = True
        elif header and line.startswith(("--- ", "+++ ")):
            old, path = _file(line, old)
        elif line.startswith("@@"):
            header = False
            _add_hunk(hunks, path, line)
    return hunks


def parse_log(text: str) -> list[Commit]:
    commits = []
    for chunk in text.split(RECORD)[1:]:
        sha, when, subject, body, diff = chunk.split("\x00", 4)
        commits.append(Commit(sha, datetime.fromisoformat(when), subject, body, _hunks(diff)))
    return commits


def in_window(when: datetime, windows: list[tuple[datetime, datetime]]) -> bool:
    return any(start <= when <= end + GRACE for start, end in windows)


def _touches(lines: dict[str, set[int]], later: Commit) -> bool:
    return any(b and any(a <= x < a + b for x in lines.get(path, ())) for path, hunks in later.hunks.items() for a, b, _, _ in hunks)


def _follow(lines: dict[str, set[int]], later: Commit) -> None:
    for path, hunks in later.hunks.items():
        if path in lines:
            lines[path] = {x + sum(d - b for a, b, _, d in hunks if x >= a + max(b, 1)) for x in lines[path]}


def reworked(target: Commit, every: list[Commit], days: int = REWORK_DAYS) -> bool:
    end = target.when + timedelta(days=days)
    names = (f'Revert "{target.subject}"', f"fixup! {target.subject}", f"squash! {target.subject}")
    lines = {path: {x for _, _, n, d in hunks for x in range(n, n + d)} for path, hunks in target.hunks.items()}
    for later in sorted((c for c in every if target.when < c.when <= end), key=lambda c: c.when):
        if later.subject in names or target.sha in later.body or _touches(lines, later):
            return True
        _follow(lines, later)
    return False


def rate(commits: int, mature: int, reworked: int) -> dict:
    return {"commits": commits, "mature": mature, "reworked": reworked,
            "rework_rate": reworked / mature if mature >= MIN_COMMITS else None}


def group_stats(group: list[Commit], every: list[Commit], now: datetime) -> dict:
    mature = [c for c in group if c.when <= now - timedelta(days=REWORK_DAYS)]
    return rate(len(group), len(mature), sum(reworked(c, every) for c in mature))


@dataclass(frozen=True)
class Session:
    id: str
    cwd: str | None
    start: datetime
    end: datetime


def _ts(text: str) -> datetime:
    return datetime.fromisoformat(text)


def _iso(when: datetime) -> str:
    return when.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def sessions(con, since: str) -> list[Session]:
    out = []
    for tid, cwd, started, last in con.execute(SESSIONS, (since,)):
        start = _ts(started)
        out.append(Session(tid, cwd, start, max(start, _ts(last)) if last else start))
    return out


def _git(*args: str) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(["git", "-c", "core.quotepath=false", *args], capture_output=True, text=True,
                              encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL, check=False)
    except OSError:
        return None


def repo_root(cwd: str | None) -> Path | None:
    if not cwd or not Path(cwd).is_dir():
        return None
    r = _git("-C", cwd, "rev-parse", "--show-toplevel")
    return Path(r.stdout.strip()).resolve() if r and r.returncode == 0 and r.stdout.strip() else None


def git_log(root: Path, since: datetime) -> list[Commit]:
    r = _git("-C", str(root), "log", "--no-merges", f"--since={_iso(since)}", LOG_FORMAT, "-p", "--unified=0",
             "--no-color", "--no-ext-diff", "--no-textconv", "--no-renames", "--src-prefix=a/", "--dst-prefix=b/")
    return parse_log(r.stdout) if r and r.returncode == 0 else []


def by_repo(sess: list[Session]) -> tuple[dict[Path, list[Session]], list[str]]:
    repos: dict[Path, list[Session]] = {}
    unreachable: list[str] = []
    roots: dict[str | None, Path | None] = {}
    for s in sess:
        if s.cwd not in roots:
            roots[s.cwd] = repo_root(s.cwd)
        root = roots[s.cwd]
        if root is None:
            unreachable.append(s.cwd or "(no cwd)")
        else:
            repos.setdefault(root, []).append(s)
    return repos, sorted(set(unreachable))


def _owners(con, since: str | None = None):
    query = "SELECT c.thread_id, t.kind, t.parent_id, c.cmd, c.exit_code, c.escalated, c.ts FROM call_view c LEFT JOIN threads t ON t.id = c.thread_id"
    for tid, kind, parent, cmd, code, esc, ts in con.execute(query):
        if since is None or (ts or "") >= since:
            yield (parent if kind != "main" and parent else tid), cmd or "", code, esc


def _skill_reads(con, since: str):
    query = ("SELECT s.skill, c.thread_id, t.kind, t.parent_id FROM skill_reads s "
             "JOIN calls c ON c.thread_id = s.thread_id AND c.call_id = s.call_id LEFT JOIN threads t ON t.id = c.thread_id WHERE c.ts >= ?")
    for skill, tid, kind, parent in con.execute(query, (since,)):
        yield skill, (parent if kind != "main" and parent else tid)


def discipline(con, owners: set[str]) -> dict:
    with_tests, runs, passed = set(), 0, 0
    for owner, cmd, code, _ in _owners(con):
        if owner in owners and is_test(cmd):
            with_tests.add(owner)
            if code is not None:
                runs += 1
                passed += code == 0
    return {"sessions": len(owners), "with_tests": len(with_tests), "test_runs": runs, "passed": passed}


def installed_skills() -> list[str]:
    root = paths.agents_skills()
    return sorted(p.parent.name for p in root.glob("*/SKILL.md")) if root.is_dir() else []


def use(con, since: str, installed: list[str]) -> dict:
    fired: dict[str, set[str]] = defaultdict(set)
    calls = escalated = 0
    for _, _, _, esc in _owners(con, since):
        calls += 1
        escalated += esc
    for skill, owner in _skill_reads(con, since):
        fired[skill].add(owner)
    skills = {k: len(v) for k, v in sorted(fired.items())}
    guardian = dict(con.execute("SELECT outcome, COUNT(*) FROM guardian WHERE ts >= ? GROUP BY outcome ORDER BY outcome", (since,)))
    return {"skills": skills, "never": sorted(set(installed) - set(skills)), "guardian": guardian,
            "calls": calls, "escalated": escalated}


def _tokens(con, since: str, ids: set[str]) -> tuple[dict[str, int], int, int]:
    tokens: dict[str, int] = defaultdict(int)
    inp = cached = 0
    for tid, kind, parent, i, c, o in con.execute("SELECT thread_id, kind, parent_id, input, cached, output FROM turn_usage WHERE ts >= ?", (since,)):
        owner = parent if kind != "main" and parent else tid
        if owner in ids:
            tokens[owner] += i + o
            inp += i
            cached += c
    return tokens, inp, cached


def cost(con, since: str, sess: list[Session], in_repos: list[Session], harness_commits: int) -> dict:
    ids, repo_ids = {s.id for s in sess}, {s.id for s in in_repos}
    tokens, inp, cached = _tokens(con, since, ids)
    total = sum(tokens.values())
    repo_total = sum(v for k, v in tokens.items() if k in repo_ids)
    minutes = [(s.end - s.start).total_seconds() / 60 for s in sess]
    return {"sessions": len(ids), "tokens": total, "tokens_per_session": total / len(ids) if ids else 0,
            "tokens_per_harness_commit": repo_total / harness_commits if harness_commits else None,
            "cache_hit": cached / inp if inp else 0.0,
            "compactions": con.execute("SELECT COUNT(*) FROM compactions WHERE ts >= ?", (since,)).fetchone()[0],
            "median_minutes": statistics.median(minutes) if minutes else 0}


def sample(commits: list[Commit], n: int) -> list[Commit]:
    if n <= 0 or not commits:
        return []
    if n >= len(commits):
        return list(commits)
    step = len(commits) / n
    return [commits[int(i * step)] for i in range(n)]


def changed_lines(c: Commit) -> int:
    return sum(d for hunks in c.hunks.values() for _, _, _, d in hunks)


def in_changed_lines(found: list, c: Commit) -> list:
    return [f for f in found if any(d and n <= f.line < n + d for _, _, n, d in c.hunks.get(f.path, ()))]


def analyze_commit(root: Path, c: Commit, timeout: int = 600, path: str | None = None) -> list | None:
    tools = [t for t in analyze.TOOLS if t.name != "gitleaks-history"]
    with tempfile.TemporaryDirectory() as tmp:
        hooks = Path(tmp) / "hooks"
        hooks.mkdir()
        wt = Path(tmp) / "wt"
        g = ("-c", f"core.hooksPath={hooks}", "-C", str(root))
        made = _git(*g, "worktree", "add", "--detach", "--quiet", str(wt), c.sha)
        if not made or made.returncode != 0:
            return None
        try:
            ran = analyze.run_tools(tools, wt, analyze.detect(wt), path=path, timeout=timeout)
            return in_changed_lines(ran.findings, c) if ran.ran and not ran.errors else None
        finally:
            _git(*g, "worktree", "remove", "--force", str(wt))


def _quality(cats: Counter, lines: int, sampled: int, failed: int) -> dict:
    return {"sampled": sampled, "failed": failed, "changed_lines": lines, "findings": dict(cats),
            "per_100_lines": {k: v * 100 / lines for k, v in cats.items()} if lines else {}}


def quality(root: Path, group: list[Commit], n: int, runner) -> dict | None:
    if n <= 0:
        return None
    picked = sample(group, n)
    cats: Counter = Counter()
    done = []
    for c in picked:
        found = runner(root, c)
        if found is not None:
            done.append(c)
            cats.update(f.category for f in found)
    return _quality(cats, sum(changed_lines(c) for c in done), len(done), len(picked) - len(done))


def _total_quality(items: list[dict | None]) -> dict | None:
    items = [q for q in items if q]
    if not items:
        return None
    cats: Counter = Counter()
    for q in items:
        cats.update(q["findings"])
    return _quality(cats, sum(q["changed_lines"] for q in items), sum(q["sampled"] for q in items), sum(q["failed"] for q in items))


def repo_report(root: Path, sess: list[Session], since: datetime, now: datetime, analyze_n: int = 0, runner=None) -> dict:
    every = git_log(root, since)
    windows = [(s.start, s.end) for s in sess]
    groups = {"harness": [c for c in every if in_window(c.when, windows)],
              "baseline": [c for c in every if not in_window(c.when, windows)]}
    runner = runner or analyze_commit
    out = {"root": str(root), "sessions": len(sess)}
    for name, group in groups.items():
        out[name] = group_stats(group, every, now) | {"quality": quality(root, group, analyze_n, runner)}
    return out


def _total(groups: list[dict]) -> dict:
    return rate(sum(g["commits"] for g in groups), sum(g["mature"] for g in groups), sum(g["reworked"] for g in groups))


def report(con, since: datetime, now: datetime, repo: Path | None = None, analyze_n: int = 0, runner=None, timeout: int = 600) -> dict:
    iso = _iso(since)
    sess = sessions(con, iso)
    repos, unreachable = by_repo(sess)
    if repo is not None:
        root = repo_root(str(repo))
        repos = {root: repos.get(root, [])} if root else {}
    runner = runner or partial(analyze_commit, timeout=timeout)
    per = [repo_report(root, s, since, now, analyze_n, runner) for root, s in sorted(repos.items())]
    totals = {g: _total([r[g] for r in per]) | {"quality": _total_quality([r[g]["quality"] for r in per])} for g in ("harness", "baseline")}
    in_repos = [s for group in repos.values() for s in group]
    return {"since": iso, "days": (now - since).days, "repos": per, "totals": totals, "unreachable": unreachable,
            "discipline": discipline(con, {s.id for s in in_repos}),
            "use": use(con, iso, installed_skills()),
            "cost": cost(con, iso, sess, in_repos, totals["harness"]["commits"])}


def _pct(a: float, b: float) -> str:
    return f"{100 * a / b:.1f}%" if b else "n/a"


def _group(name: str, g: dict) -> list[str]:
    if g["rework_rate"] is None:
        rework = f"rework insufficient (n={g['mature']} mature, need {MIN_COMMITS})"
    else:
        rework = f"rework {_pct(g['reworked'], g['mature'])} ({g['reworked']} of {g['mature']} mature)"
    lines = [f"  {name:<9} commits {g['commits']}, {rework}"]
    q = g.get("quality")
    if q:
        cats = ", ".join(f"{k} {v:.1f}" for k, v in sorted(q["per_100_lines"].items())) or "none"
        failed = f", {q['failed']} could not be analyzed" if q["failed"] else ""
        lines.append(f"  {'':<9} findings per 100 changed lines: {cats} ({q['sampled']} commits, {q['changed_lines']} lines{failed})")
    return lines


def _usage_lines(d: dict, u: dict) -> list[str]:
    skills = ", ".join(f"{n} {v}" for n, v in u["skills"].items()) or "none fired"
    never = f"; never fired: {', '.join(u['never'])}" if u["never"] else ""
    guardian = ", ".join(f"{n} {v}" for n, v in u["guardian"].items()) or "no reviews"
    return [f"Testing   {d['with_tests']} of {d['sessions']} sessions ran tests; {d['passed']} of {d['test_runs']} runs passed ({_pct(d['passed'], d['test_runs'])})",
            f"Skills    {skills}{never}",
            f"Guardian  {guardian}; {u['escalated']} of {u['calls']} calls escalated ({_pct(u['escalated'], u['calls'])})"]


def _cost_line(k: dict) -> str:
    per_commit = f", {k['tokens_per_harness_commit']:,.0f} per harness commit" if k["tokens_per_harness_commit"] else ""
    return (f"Cost      {k['sessions']} sessions, {k['tokens']:,} tokens ({k['tokens_per_session']:,.0f} per session{per_commit}), "
            f"cache {100 * k['cache_hit']:.1f}%, {k['compactions']} compactions, median {k['median_minutes']:.0f} min")


def render(data: dict) -> list[str]:
    out = [f"Evidence for the last {data['days']} days (since {data['since']})", ""]
    for r in data["repos"]:
        out.append(f"{analyze.printable(r['root'])}  ({r['sessions']} sessions)")
        out += _group("harness", r["harness"]) + _group("baseline", r["baseline"])
    out.append("All repos")
    out += _group("harness", data["totals"]["harness"]) + _group("baseline", data["totals"]["baseline"])
    out += ["", *_usage_lines(data["discipline"], data["use"]), _cost_line(data["cost"])]
    if data["unreachable"]:
        out += ["", "Sessions outside any git repo: " + ", ".join(analyze.printable(p) for p in data["unreachable"])]
    return out + ["", CAVEAT]


def cli(args) -> int:
    analyze.utf8_stdout()
    repo = None
    if args.repo:
        repo = repo_root(args.repo)
        if repo is None:
            print(f"code-jig: {args.repo} is not a git repository", file=sys.stderr)
            return 2
    home = paths.codex_home(args.codex_home)
    con = collect.connect(collect.db_path())
    try:
        collect.scan(con, home)
        now = datetime.now(UTC)
        data = report(con, now - timedelta(days=args.days), now, repo, args.analyze, timeout=args.timeout)
    finally:
        con.close()
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=1))
    else:
        for line in render(data):
            print(line)
    return 0
