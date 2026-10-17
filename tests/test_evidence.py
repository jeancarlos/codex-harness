import contextlib
import io
import json
import os
import shutil
import subprocess
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

from codejig import collect, evidence
from codejig.analyze import Finding
from codejig.evidence import (
    Commit,
    group_stats,
    in_window,
    is_test,
    parse_log,
    reworked,
)
from tests.test_collect import home_with
from tests.util import tree, write

T0 = datetime(2026, 3, 1, 10, 0, tzinfo=UTC)


def c(sha, days, subject="change", body="", hunks=None):
    return Commit(sha, T0 + timedelta(days=days), subject, body, hunks or {})


class TestCommands(unittest.TestCase):
    def test_is_test(self):
        for cmd in ("pytest -q", "python -m unittest discover", "dotnet test", "npm test", "pnpm test", "yarn test",
                    "npm run test -- --ci", "npx vitest run", "npx jest"):
            with self.subTest(cmd=cmd):
                self.assertTrue(is_test(cmd))
        for cmd in ("git status", "npm install", "dotnet build", "ls tests"):
            with self.subTest(cmd=cmd):
                self.assertFalse(is_test(cmd))


class Windows(unittest.TestCase):
    def test_inside_grace_and_outside(self):
        w = [(T0, T0 + timedelta(hours=1))]
        self.assertTrue(in_window(T0 + timedelta(minutes=30), w))
        self.assertTrue(in_window(T0 + timedelta(hours=2, minutes=59), w))
        self.assertFalse(in_window(T0 + timedelta(hours=3, minutes=1), w))
        self.assertFalse(in_window(T0 - timedelta(minutes=1), w))
        self.assertFalse(in_window(T0, []))


class ParseLog(unittest.TestCase):
    def test_parse_log_hunks_and_fields(self):
        text = ("\x00\x01aaa\x002026-03-01T10:00:00+00:00\x00add\x00body line\n\x00\n"
                "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -1,0 +1,3 @@\n+a\n+b\n+c\n@@ -9 +12 @@\n-o\n+n\n"
                "\x00\x01bbb\x002026-03-02T10:00:00-03:00\x00second\x00\x00")
        a, b = parse_log(text)
        self.assertEqual((a.sha, a.subject, a.body.strip()), ("aaa", "add", "body line"))
        self.assertEqual(a.hunks, {"x.py": [(1, 0, 1, 3), (9, 1, 12, 1)]})
        self.assertEqual(b.when, datetime(2026, 3, 2, 13, 0, tzinfo=UTC))
        self.assertEqual(b.hunks, {})

    def test_parse_log_binary_and_deleted_files(self):
        text = ("\x00\x01ccc\x002026-03-01T10:00:00+00:00\x00mixed\x00\x00\n"
                "diff --git a/img.png b/img.png\nindex 1..2 100644\nBinary files a/img.png and b/img.png differ\n"
                "diff --git a/gone.py b/gone.py\ndeleted file mode 100644\n--- a/gone.py\n+++ /dev/null\n@@ -1,2 +0,0 @@\n-x\n-y\n")
        [commit] = parse_log(text)
        self.assertEqual(commit.hunks, {"gone.py": [(1, 2, 0, 0)]})

    def test_parse_log_content_that_looks_like_headers(self):
        text = ("\x00\x01ddd\x002026-03-01T10:00:00+00:00\x00sql\x00\x00\n"
                "diff --git a/q.sql b/q.sql\n--- a/q.sql\n+++ b/q.sql\n@@ -3 +3 @@\n--- old comment\n+++ new comment\n@@ -9,0 +10 @@\n+x\n")
        [commit] = parse_log(text)
        self.assertEqual(commit.hunks, {"q.sql": [(3, 1, 3, 1), (9, 0, 10, 1)]})

    def test_parse_log_empty(self):
        self.assertEqual(parse_log(""), [])


class Rework(unittest.TestCase):
    def test_same_lines_within_14_days(self):
        target = c("t", 0, hunks={"x.py": [(0, 0, 10, 5)]})
        later = c("l", 5, hunks={"x.py": [(12, 1, 12, 1)]})
        self.assertTrue(reworked(target, [later, target]))

    def test_other_lines_or_other_file_or_too_late(self):
        target = c("t", 0, hunks={"x.py": [(0, 0, 10, 5)]})
        self.assertFalse(reworked(target, [c("a", 5, hunks={"x.py": [(20, 2, 20, 2)]})]))
        self.assertFalse(reworked(target, [c("b", 5, hunks={"y.py": [(12, 1, 12, 1)]})]))
        self.assertFalse(reworked(target, [c("d", 15, hunks={"x.py": [(12, 1, 12, 1)]})]))
        self.assertFalse(reworked(target, [c("e", 5, hunks={"x.py": [(12, 0, 13, 4)]})]))

    def test_lines_are_followed_through_intervening_commits(self):
        target = c("t", 0, hunks={"x.py": [(0, 0, 10, 5)]})
        shift = c("i", 2, hunks={"x.py": [(0, 0, 1, 20)]})
        self.assertFalse(reworked(target, [c("old", 5, hunks={"x.py": [(12, 1, 12, 1)]}), shift, target]))
        self.assertTrue(reworked(target, [c("new", 5, hunks={"x.py": [(32, 1, 32, 1)]}), shift, target]))
        cut = c("j", 2, hunks={"x.py": [(1, 5, 1, 0)]})
        self.assertTrue(reworked(target, [c("new", 5, hunks={"x.py": [(7, 1, 7, 1)]}), cut]))

    def test_revert_fixup_squash(self):
        target = c("abc123", 0, subject="add parser")
        self.assertTrue(reworked(target, [c("r", 1, 'Revert "add parser"', "This reverts commit abc123.")]))
        self.assertTrue(reworked(target, [c("f", 1, "fixup! add parser")]))
        self.assertTrue(reworked(target, [c("s", 1, "squash! add parser")]))
        self.assertFalse(reworked(target, [c("o", 1, "add parser tests")]))

    def test_earlier_commits_never_count(self):
        target = c("t", 3, hunks={"x.py": [(0, 0, 10, 5)]})
        self.assertFalse(reworked(target, [c("early", 1, hunks={"x.py": [(12, 1, 12, 1)]})]))


class Groups(unittest.TestCase):
    def test_young_commits_are_not_mature(self):
        now = T0 + timedelta(days=20)
        group = [c("old", 0), c("young", 10)]
        stats = group_stats(group, group, now)
        self.assertEqual((stats["commits"], stats["mature"]), (2, 1))

    def test_insufficient_below_twenty(self):
        now = T0 + timedelta(days=100)
        group = [c(f"s{i}", 0) for i in range(19)]
        self.assertIsNone(group_stats(group, group, now)["rework_rate"])
        group.append(c("s19", 0))
        self.assertEqual(group_stats(group, group, now)["rework_rate"], 0.0)

    def test_rate(self):
        self.assertEqual(evidence.rate(30, 25, 5), {"commits": 30, "mature": 25, "reworked": 5, "rework_rate": 0.2})
        self.assertIsNone(evidence.rate(5, 5, 1)["rework_rate"])


GIT_ENV = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1", "GIT_AUTHOR_NAME": "t",
           "GIT_AUTHOR_EMAIL": "t@example.invalid", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}


def git(root, *args, date=None):
    env = {**os.environ, **GIT_ENV}
    if date:
        env |= {"GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date}
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, env=env)


def commit(root, name, text, date, msg="change"):
    write(root / name, text)
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", msg, date=date)


def new_repo(test):
    root = tree(test) / "repo"
    root.mkdir()
    git(root, "init", "-q")
    return root


def db(test, sessions=(), calls=(), usage=(), guardian=()):
    con = collect.connect(tree(test) / "usage.sqlite3")
    test.addCleanup(con.close)
    for tid, kind, parent, started, cwd in sessions:
        con.execute("INSERT INTO threads VALUES (?,?,?,?,?)", (tid, parent, kind, started, cwd))
    for tid, cid, ts, cmd, esc, code, skill in calls:
        con.execute("INSERT INTO calls VALUES (?,?,NULL,?,?,?)", (tid, cid, ts, cmd, esc))
        con.execute("INSERT INTO call_results (thread_id, call_id, exit_code) VALUES (?,?,?)", (tid, cid, code))
        if skill:
            con.execute("INSERT INTO skill_reads VALUES (?,?,?)", (tid, cid, skill))
    for tid, key, ts, inp, cached, out in usage:
        con.execute("INSERT INTO usage VALUES (?,?,NULL,?,NULL,?,?,?,0)", (tid, key, ts, inp, cached, out))
    for tid, line, ts, outcome in guardian:
        con.execute("INSERT INTO guardian VALUES (?,?,?,?)", (tid, line, ts, outcome))
    con.commit()
    return con


@unittest.skipIf(shutil.which("git") is None, "git not installed")
class GitRepos(unittest.TestCase):
    def test_split_by_session_window(self):
        root = new_repo(self)
        (root / "sub").mkdir()
        commit(root, "a.py", "1\n", "2026-01-01T10:00:00Z", "before")
        commit(root, "a.py", "2\n", "2026-03-01T10:30:00Z", "inside")
        commit(root, "a.py", "3\n", "2026-03-01T12:30:00Z", "grace")
        commit(root, "a.py", "4\n", "2026-03-01T14:00:00Z", "after")
        con = db(self, sessions=[("s1", "main", None, "2026-03-01T10:00:00Z", str(root / "sub"))],
                 usage=[("s1", "r1", "2026-03-01T11:00:00Z", 100, 50, 10)])
        now = T0 + timedelta(days=200)
        data = evidence.report(con, datetime(2025, 12, 1, tzinfo=UTC), now)
        [repo] = data["repos"]
        self.assertEqual(Path(repo["root"]), root.resolve())
        self.assertEqual((repo["harness"]["commits"], repo["baseline"]["commits"]), (2, 2))

    def test_offset_commit_dates_compare_as_instants(self):
        root = new_repo(self)
        commit(root, "a.py", "1\n", "2026-03-01T07:30:00-03:00")
        con = db(self, sessions=[("s1", "main", None, "2026-03-01T10:00:00Z", str(root))],
                 usage=[("s1", "r1", "2026-03-01T11:00:00Z", 1, 0, 1)])
        data = evidence.report(con, datetime(2026, 2, 1, tzinfo=UTC), T0 + timedelta(days=30))
        self.assertEqual(data["repos"][0]["harness"]["commits"], 1)

    def test_non_ascii_paths_in_hunks(self):
        root = new_repo(self)
        commit(root, "dir with space/ação.py", "x = 1\n", "2026-03-01T10:00:00Z")
        [c0] = evidence.git_log(root, datetime(2026, 1, 1, tzinfo=UTC))
        self.assertEqual(list(c0.hunks), ["dir with space/ação.py"])

    def test_rework_through_real_git(self):
        root = new_repo(self)
        commit(root, "a.py", "1\n2\n3\n", "2026-03-01T10:00:00Z", "add a")
        commit(root, "a.py", "1\nTWO\n3\n", "2026-03-05T10:00:00Z", "tweak")
        commit(root, "b.py", "b\n", "2026-03-06T10:00:00Z", "add b")
        git(root, "revert", "--no-edit", "HEAD", date="2026-03-07T10:00:00Z")
        every = evidence.git_log(root, datetime(2026, 1, 1, tzinfo=UTC))
        by = {c0.subject: c0 for c0 in every}
        self.assertTrue(evidence.reworked(by["add a"], every))
        self.assertTrue(evidence.reworked(by["add b"], every))
        self.assertFalse(evidence.reworked(by["tweak"], every))

    def test_missing_cwd_and_empty_repo(self):
        empty = new_repo(self)
        con = db(self, sessions=[("gone", "main", None, "2026-03-01T10:00:00Z", str(empty.parent / "deleted")),
                                 ("plain", "main", None, "2026-03-01T10:00:00Z", str(tree(self))),
                                 ("empty", "main", None, "2026-03-01T10:00:00Z", str(empty)),
                                 ("none", "main", None, "2026-03-01T10:00:00Z", None)])
        data = evidence.report(con, datetime(2026, 2, 1, tzinfo=UTC), T0)
        self.assertEqual(len(data["unreachable"]), 3)
        self.assertEqual(data["repos"][0]["harness"]["commits"], 0)

    def test_repo_filter(self):
        root = new_repo(self)
        commit(root, "a.py", "1\n", "2026-03-01T10:00:00Z")
        con = db(self)
        data = evidence.report(con, datetime(2026, 2, 1, tzinfo=UTC), T0, repo=root)
        self.assertEqual([r["baseline"]["commits"] for r in data["repos"]], [1])


class FromDatabase(unittest.TestCase):
    def setUp(self):
        self.con = db(self,
                      sessions=[("s1", "main", None, "2026-03-01T10:00:00Z", None), ("sub", "subagent", "s1", "2026-03-01T10:05:00Z", None),
                                ("s2", "main", None, "2026-03-02T10:00:00Z", None), ("g", "guardian", "s1", "2026-03-01T10:06:00Z", None)],
                      calls=[("s1", "c1", "2026-03-01T10:01:00Z", "python -m unittest", 0, 0, None),
                             ("sub", "c2", "2026-03-01T10:06:00Z", "pytest -q", 0, 1, None),
                             ("s1", "c3", "2026-03-01T10:07:00Z", "cat x/skills/bug-hunt/SKILL.md", 0, 0, "bug-hunt"),
                             ("s2", "c4", "2026-03-02T10:01:00Z", "curl -sI https://example.com", 1, 0, None),
                             ("s2", "c5", "2026-03-02T10:02:00Z", "cat y/skills/bug-hunt/SKILL.md", 0, 0, "bug-hunt")],
                      usage=[("s1", "r1", "2026-03-01T10:30:00Z", 1000, 900, 100), ("sub", "r2", "2026-03-01T10:20:00Z", 500, 400, 50),
                             ("g", "r3", "2026-03-01T10:06:30Z", 300, 0, 10), ("s2", "r4", "2026-03-02T10:10:00Z", 200, 100, 20)],
                      guardian=[("g", 3, "2026-03-01T10:06:40Z", "allow")])
        self.since = "2026-02-01T00:00:00Z"

    def test_sessions_span_children(self):
        sess = {s.id: s for s in evidence.sessions(self.con, self.since)}
        self.assertEqual(set(sess), {"s1", "s2"})
        self.assertEqual(sess["s1"].end, datetime(2026, 3, 1, 10, 30, tzinfo=UTC))

    def test_discipline_counts_subagent_runs_for_the_owner(self):
        self.assertEqual(evidence.discipline(self.con, {"s1", "s2"}),
                         {"sessions": 2, "with_tests": 1, "test_runs": 2, "passed": 1})

    def test_test_run_without_exit_code(self):
        self.con.execute("INSERT INTO calls (thread_id, call_id, ts, cmd, escalated) VALUES ('s2', 'c6', '2026-03-02T10:03:00Z', 'npm test', 0)")
        self.assertEqual(evidence.discipline(self.con, {"s1", "s2"}),
                         {"sessions": 2, "with_tests": 2, "test_runs": 2, "passed": 1})

    def test_use(self):
        u = evidence.use(self.con, self.since, ["bug-hunt", "codegen", "grilling"])
        self.assertEqual(u, {"skills": {"bug-hunt": 2}, "never": ["codegen", "grilling"], "guardian": {"allow": 1},
                             "calls": 5, "escalated": 1})

    def test_cost(self):
        sess = evidence.sessions(self.con, self.since)
        s1 = [s for s in sess if s.id == "s1"]
        k = evidence.cost(self.con, self.since, sess, s1, 2)
        self.assertEqual(k["tokens"], 1000 + 100 + 500 + 50 + 300 + 10 + 200 + 20)
        self.assertEqual(k["tokens_per_harness_commit"], (1000 + 100 + 500 + 50 + 300 + 10) / 2)
        self.assertAlmostEqual(k["cache_hit"], (900 + 400 + 0 + 100) / (1000 + 500 + 300 + 200))
        self.assertEqual(k["median_minutes"], 20.0)

    def test_installed_skills(self):
        root = tree(self)
        for name in ("b", "a"):
            write(root / name / "SKILL.md", "x\n")
        (root / "not-a-skill").mkdir()
        with mock.patch("codejig.paths.agents_skills", return_value=root):
            self.assertEqual(evidence.installed_skills(), ["a", "b"])


class Quality(unittest.TestCase):
    def test_sample_is_even_and_bounded(self):
        group = [c(str(i), i) for i in range(10)]
        self.assertEqual([x.sha for x in evidence.sample(group, 3)], ["0", "3", "6"])
        self.assertEqual(len(evidence.sample(group, 50)), 10)
        self.assertEqual(evidence.sample(group, 0), [])

    def test_quality_counts_per_100_changed_lines(self):
        group = [c("a", 0, hunks={"x.py": [(0, 0, 1, 30)]}), c("b", 1, hunks={"y.py": [(0, 0, 1, 20)]})]
        found = {"a": [Finding("security", "ruff", "S1", "x.py", 1, "m")], "b": [Finding("complexity", "lizard", "c", "y.py", 1, "m")] * 2}
        q = evidence.quality(Path("."), group, 5, lambda root, commit: found[commit.sha])
        self.assertEqual(q, {"sampled": 2, "failed": 0, "changed_lines": 50, "findings": {"security": 1, "complexity": 2},
                             "per_100_lines": {"security": 2.0, "complexity": 4.0}})
        self.assertIsNone(evidence.quality(Path("."), group, 0, None))

    @unittest.skipIf(shutil.which("git") is None, "git not installed")
    def test_analyze_commit_cleans_its_worktree(self):
        root = new_repo(self)
        write(root / "pyproject.toml", "[project]\nname='x'\n")
        commit(root, "a.py", "x = 1\n", "2026-03-01T10:00:00Z")
        [c0] = evidence.git_log(root, datetime(2026, 1, 1, tzinfo=UTC))
        empty = tree(self)
        self.assertIsNone(evidence.analyze_commit(root, c0, path=str(empty)))
        listed = subprocess.run(["git", "-C", str(root), "worktree", "list"], capture_output=True, text=True, encoding="utf-8", check=False).stdout
        self.assertEqual(len(listed.strip().splitlines()), 1)

    @unittest.skipIf(shutil.which("git") is None, "git not installed")
    def test_report_carries_quality_when_asked(self):
        root = new_repo(self)
        commit(root, "a.py", "1\n", "2026-03-01T10:30:00Z")
        con = db(self, sessions=[("s1", "main", None, "2026-03-01T10:00:00Z", str(root))],
                 usage=[("s1", "r1", "2026-03-01T11:00:00Z", 1, 0, 1)])
        runner = lambda r, commit: [Finding("bug", "ruff", "B1", "a.py", 1, "m")]
        data = evidence.report(con, datetime(2026, 2, 1, tzinfo=UTC), T0 + timedelta(days=30), analyze_n=1, runner=runner)
        self.assertEqual(data["repos"][0]["harness"]["quality"]["findings"], {"bug": 1})
        self.assertEqual(data["totals"]["harness"]["quality"]["findings"], {"bug": 1})
        self.assertIsNone(evidence.report(con, datetime(2026, 2, 1, tzinfo=UTC), T0)["repos"][0]["harness"]["quality"])


class Cli(unittest.TestCase):
    def run_cli(self, *argv):
        from codejig.__main__ import main
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_json_shape_and_collect_first(self):
        home = home_with(tree(self), ["main", "guardian", "tools"])
        code, out, _ = self.run_cli("--codex-home", str(home), "evidence", "--json", "--days", "100000")
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertEqual(set(data), {"since", "days", "repos", "totals", "unreachable", "discipline", "use", "cost"})
        self.assertEqual(data["unreachable"], ["/w"])
        self.assertEqual(data["use"]["guardian"], {"allow": 1})
        self.assertEqual(data["use"]["skills"], {"bug-hunt": 1, "code-analysis": 1, "grilling": 1, "regression-tests": 1, "using-superpowers": 1})
        self.assertTrue(collect.db_path().is_file())

    def test_text_report_marks_insufficient_and_caveat(self):
        home = home_with(tree(self), ["main"])
        code, out, _ = self.run_cli("--codex-home", str(home), "evidence", "--days", "100000")
        self.assertEqual(code, 0)
        self.assertIn("insufficient (n=0", out)
        self.assertIn("correlation, not causation", out)
        self.assertIn("/w", out)

    def test_repo_that_is_not_git_is_a_usage_error(self):
        home = home_with(tree(self), [])
        code, _, err = self.run_cli("--codex-home", str(home), "evidence", "--repo", str(tree(self)))
        self.assertEqual(code, 2)
        self.assertIn("not a git repository", err)

    def test_render_rates(self):
        group = {"commits": 30, "mature": 25, "reworked": 5, "rework_rate": 0.2, "quality": None}
        data = {"since": "2026-01-01T00:00:00Z", "days": 90, "repos": [], "unreachable": [],
                "totals": {"harness": group, "baseline": group},
                "discipline": {"sessions": 4, "with_tests": 3, "test_runs": 10, "passed": 8},
                "use": {"skills": {"bug-hunt": 2}, "never": ["codegen"], "guardian": {"allow": 3}, "calls": 40, "escalated": 4},
                "cost": {"sessions": 4, "tokens": 4_000_000, "tokens_per_session": 1_000_000, "tokens_per_harness_commit": 133_333,
                         "cache_hit": 0.94, "compactions": 2, "median_minutes": 18.0}}
        text = "\n".join(evidence.render(data))
        for part in ("rework 20.0% (5 of 25 mature)", "3 of 4 sessions ran tests", "8 of 10 runs passed (80.0%)",
                     "bug-hunt 2", "never fired: codegen", "allow 3", "4 of 40 calls escalated (10.0%)", "cache 94.0%"):
            with self.subTest(part=part):
                self.assertIn(part, text)


class ReviewFixes(unittest.TestCase):
    def test_quoted_path_header(self):
        text = ('\x00\x01eee\x002026-03-01T10:00:00+00:00\x00q\x00\x00\n'
                'diff --git "a/q\\"uote.txt" "b/q\\"uote.txt"\nnew file mode 100644\n--- /dev/null\n+++ "b/q\\"uote.txt"\n@@ -0,0 +1 @@\n+x\n'
                'diff --git "a/tab\\there.txt" "b/tab\\there.txt"\n--- "a/tab\\there.txt"\n+++ "b/tab\\there.txt"\n@@ -2 +2 @@\n-a\n+b\n')
        [commit] = parse_log(text)
        self.assertEqual(commit.hunks, {'q"uote.txt': [(0, 0, 1, 1)], "tab\there.txt": [(2, 1, 2, 1)]})

    def test_findings_outside_changed_lines_do_not_count(self):
        c0 = c("a", 0, hunks={"x.py": [(0, 0, 10, 3)], "z.py": [(5, 2, 4, 0)]})
        keep = Finding("bug", "ruff", "B1", "x.py", 11, "m")
        found = [keep, Finding("bug", "ruff", "B1", "x.py", 50, "m"), Finding("bug", "ruff", "B1", "y.py", 11, "m"),
                 Finding("bug", "ruff", "B1", "x.py", 0, "m"), Finding("bug", "ruff", "B1", "z.py", 4, "m")]
        self.assertEqual(evidence.in_changed_lines(found, c0), [keep])

    def test_failed_commits_are_not_sampled(self):
        group = [c("a", 0, hunks={"x.py": [(0, 0, 1, 10)]}), c("b", 1, hunks={"y.py": [(0, 0, 1, 10)]})]
        q = evidence.quality(Path("."), group, 5, lambda root, commit: None if commit.sha == "b" else [])
        self.assertEqual((q["sampled"], q["failed"], q["changed_lines"]), (1, 1, 10))


@unittest.skipIf(shutil.which("git") is None, "git not installed")
class ReviewFixesGit(unittest.TestCase):
    def test_control_byte_in_content_does_not_split_records(self):
        root = new_repo(self)
        commit(root, "fix.txt", "8=FIX\x019=12\n", "2026-03-01T10:00:00Z")
        commit(root, "b.txt", "b\n", "2026-03-02T10:00:00Z")
        every = evidence.git_log(root, datetime(2026, 1, 1, tzinfo=UTC))
        self.assertEqual(sorted(p for c0 in every for p in c0.hunks), ["b.txt", "fix.txt"])

    def test_noprefix_config_keeps_hunks(self):
        root = new_repo(self)
        commit(root, "a.py", "1\n", "2026-03-01T10:00:00Z")
        extra = {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "diff.noprefix", "GIT_CONFIG_VALUE_0": "true"}
        with mock.patch.dict(os.environ, extra):
            [c0] = evidence.git_log(root, datetime(2026, 1, 1, tzinfo=UTC))
        self.assertEqual(list(c0.hunks), ["a.py"])

    def test_rebased_session_commit_stays_harness(self):
        root = new_repo(self)
        write(root / "a.py", "1\n")
        git(root, "add", "-A")
        env = {**os.environ, **GIT_ENV, "GIT_AUTHOR_DATE": "2026-03-01T10:30:00Z", "GIT_COMMITTER_DATE": "2026-03-01T15:00:00Z"}
        subprocess.run(["git", "-C", str(root), "commit", "-q", "-m", "rebased"], check=True, capture_output=True, env=env)
        con = db(self, sessions=[("s1", "main", None, "2026-03-01T10:00:00Z", str(root))],
                 usage=[("s1", "r1", "2026-03-01T11:00:00Z", 1, 0, 1)])
        data = evidence.report(con, datetime(2026, 2, 1, tzinfo=UTC), T0 + timedelta(days=30))
        self.assertEqual((data["repos"][0]["harness"]["commits"], data["repos"][0]["baseline"]["commits"]), (1, 0))
