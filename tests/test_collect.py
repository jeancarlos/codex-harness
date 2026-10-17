import json
import shutil
import sqlite3
import unittest
from pathlib import Path

from codejig import collect
from tests.util import tree, write

FIX = Path(__file__).parent / "fixtures" / "rollouts"


def home_with(root, names, sub="sessions/2026/10/10"):
    home = root / "codex home"
    for n in names:
        dst = home / sub / f"rollout-2026-10-10T10-00-00-{n}.jsonl"
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(FIX / f"{n}.jsonl", dst)
    return home


def totals(con, thread):
    return con.execute("SELECT SUM(input), SUM(cached), SUM(output) FROM turn_usage WHERE thread_id=?", (thread,)).fetchone()


class Scan(unittest.TestCase):
    def setUp(self):
        self.root = tree(self)
        self.home = home_with(self.root, ["main", "guardian", "subagent", "legacy", "bad"])
        self.con = collect.connect(collect.db_path())
        self.addCleanup(self.con.close)

    def test_records_win_over_token_count(self):
        collect.scan(self.con, self.home)
        self.assertEqual(totals(self.con, "main-1"), (6000, 4300, 160))

    def test_token_counts_fill_turns_that_have_no_record(self):
        home = home_with(self.root / "mixed", [])
        lines = (FIX / "main.jsonl").read_text(encoding="utf-8").splitlines(keepends=True)
        write(home / "sessions" / "rollout-m.jsonl", "".join(ln for ln in lines if '"response_id":"r2"' not in ln))
        con = collect.connect(home / "usage.sqlite3")
        self.addCleanup(con.close)
        collect.scan(con, home)
        self.assertEqual(totals(con, "main-1"), (6000, 4300, 160))

    def test_legacy_uses_last_usage_and_skips_duplicates(self):
        collect.scan(self.con, self.home)
        self.assertEqual(totals(self.con, "old-1"), (230, 100, 70))

    def test_kinds_and_parents(self):
        collect.scan(self.con, self.home)
        rows = dict(self.con.execute("SELECT id, kind || ':' || COALESCE(parent_id, '') FROM threads"))
        self.assertEqual(rows["main-1"], "main:")
        self.assertEqual(rows["g-1"], "guardian:main-1")
        self.assertEqual(rows["sub-1"], "subagent:main-1")

    def test_model_follows_turn_context(self):
        collect.scan(self.con, self.home)
        models = dict(self.con.execute("SELECT key, model FROM turn_usage WHERE thread_id='main-1'"))
        self.assertEqual(models, {"r1": "m-a", "r2": "m-a", "r3": "m-b"})

    def test_calls_compactions(self):
        collect.scan(self.con, self.home)
        calls = self.con.execute("SELECT call_id, cmd, escalated, turn_id FROM calls ORDER BY call_id").fetchall()
        self.assertEqual(calls, [("c1", "git status", 0, "t1"), ("c2", "curl -sI https://example.com", 1, "t2"), ("c3", "sleep 30", 0, "t3")])
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM compactions").fetchone()[0], 1)

    def test_quarantine_keeps_going(self):
        summary = collect.scan(self.con, self.home)
        self.assertEqual(summary["quarantined"], 2)
        self.assertEqual(totals(self.con, "bad-1"), (10, 0, 1))
        reasons = [r for (r,) in self.con.execute("SELECT reason FROM quarantine ORDER BY line")]
        self.assertEqual(reasons, ["not JSON", "token_usage_record shape"])

    def test_idempotent_rescan_and_archive_move(self):
        collect.scan(self.con, self.home)
        collect.scan(self.con, self.home, rescan=True)
        src = next((self.home / "sessions").rglob("*-main.jsonl"))
        (self.home / "archived_sessions").mkdir()
        src.rename(self.home / "archived_sessions" / src.name)
        collect.scan(self.con, self.home)
        self.assertEqual(totals(self.con, "main-1"), (6000, 4300, 160))

    def test_partial_last_line_waits(self):
        home = home_with(self.root / "p", [])
        f = home / "sessions" / "rollout-x.jsonl"
        lines = (FIX / "subagent.jsonl").read_text(encoding="utf-8").splitlines(keepends=True)
        write(f, lines[0] + lines[1] + lines[2].rstrip("\n")[:40])
        con = collect.connect(home / "usage.sqlite3")
        self.addCleanup(con.close)
        collect.scan(con, home)
        self.assertEqual(totals(con, "sub-1"), (None, None, None))
        write(f, "".join(lines))
        collect.scan(con, home)
        self.assertEqual(totals(con, "sub-1"), (500, 0, 5))

    def test_appended_lines_are_read_incrementally(self):
        home = home_with(self.root / "a", [])
        f = home / "sessions" / "rollout-y.jsonl"
        lines = (FIX / "main.jsonl").read_text(encoding="utf-8").splitlines(keepends=True)
        write(f, "".join(lines[:5]))
        con = collect.connect(home / "usage.sqlite3")
        self.addCleanup(con.close)
        collect.scan(con, home)
        write(f, "".join(lines))
        collect.scan(con, home)
        self.assertEqual(totals(con, "main-1"), (6000, 4300, 160))


    def test_a_replaced_rollout_is_read_again_from_the_start(self):
        home = home_with(self.root / "r", [])
        f = home / "sessions" / "rollout-r.jsonl"
        text = (FIX / "main.jsonl").read_text(encoding="utf-8")
        write(f, text)
        con = collect.connect(home / "usage.sqlite3")
        self.addCleanup(con.close)
        collect.scan(con, home)
        write(f, text.replace("main-1", "main-2"))
        collect.scan(con, home)
        self.assertEqual(totals(con, "main-2"), (6000, 4300, 160))
        write(f, text.replace("main-1", "main-3") + text.replace("main-1", "main-3"))
        collect.scan(con, home)
        self.assertEqual(con.execute("SELECT kind FROM threads WHERE id='main-3'").fetchone(), ("main",))

    def test_an_offset_without_a_mark_is_still_trusted(self):
        home = home_with(self.root / "o", [])
        f = home / "sessions" / "rollout-o.jsonl"
        lines = (FIX / "main.jsonl").read_text(encoding="utf-8").splitlines(keepends=True)
        write(f, "".join(lines[:5]))
        con = collect.connect(home / "usage.sqlite3")
        self.addCleanup(con.close)
        collect.scan(con, home)
        con.execute("DELETE FROM file_marks")
        write(f, "".join(lines))
        collect.scan(con, home)
        self.assertEqual(totals(con, "main-1"), (6000, 4300, 160))
        self.assertEqual(con.execute("SELECT COUNT(*) FROM quarantine").fetchone()[0], 0)


class ToolCall(unittest.TestCase):
    def test_forms(self):
        self.assertEqual(collect.tool_call({"type": "function_call", "call_id": "a", "arguments": '{"command": ["git", "push"], "sandbox_permissions": "require_escalated"}'}), ("a", "git push", True))
        self.assertIsNone(collect.tool_call({"type": "function_call", "call_id": "a", "arguments": "{bad"}))
        self.assertIsNone(collect.tool_call({"type": "custom_tool_call", "call_id": "a", "input": "text(await tools.view_image({path:\"x\"}))"}))
        self.assertEqual(collect.tool_call({"type": "custom_tool_call", "call_id": "b", "input": 'tools.exec_command({cmd:"echo \\"hi\\""})'}), ("b", 'echo "hi"', False))
        self.assertIsNone(collect.tool_call({"type": "message"}))

    def test_each_command_in_a_cell_keeps_its_own_escalation(self):
        cell = ('await tools.exec_command({cmd:"ls"}); '
                'await tools.exec_command({sandbox_permissions:"require_escalated", cmd:"curl -sI https://x"}); '
                'await tools.exec_command({cmd:"git push", sandbox_permissions:"require_escalated", justification:"push"}); '
                'await tools.exec_command({cmd:"pwd"})')
        p = {"type": "custom_tool_call", "call_id": "m", "input": cell}
        self.assertEqual(collect.tool_call(p), ("m", "ls", False))
        self.assertEqual(collect.cell_commands(p), [("ls", False), ("curl -sI https://x", True), ("git push", True), ("pwd", False)])
        con = collect.connect(tree(self) / "usage.sqlite3")
        self.addCleanup(con.close)
        collect._response_item(con, "t", None, None, p)
        self.assertEqual(con.execute("SELECT call_id, escalated FROM calls ORDER BY call_id").fetchall(),
                         [("m", 0), ("m#2", 1), ("m#3", 1), ("m#4", 0)])


class Robustness(unittest.TestCase):
    def setUp(self):
        self.root = tree(self)

    def scan(self, names):
        home = home_with(self.root / names[0], names)
        con = collect.connect(collect.db_path())
        self.addCleanup(con.close)
        return con, collect.scan(con, home)

    def test_fork_keeps_first_session_meta(self):
        con, _ = self.scan(["forked"])
        self.assertEqual(totals(con, "fork-1"), (7, 0, 1))
        self.assertEqual(totals(con, "main-1"), (None, None, None))
        self.assertEqual(con.execute("SELECT kind FROM threads WHERE id='fork-1'").fetchone()[0], "subagent")

    def test_poison_lines_are_quarantined_and_later_files_still_collected(self):
        con, summary = self.scan(["poison", "subagent"])
        self.assertEqual(totals(con, "poison-1"), (5, 0, 1))
        self.assertEqual(totals(con, "sub-1"), (500, 0, 5))
        self.assertGreaterEqual(summary["quarantined"], 2)


class Tools(unittest.TestCase):
    def setUp(self):
        self.root = tree(self)
        self.home = home_with(self.root, ["tools", "guardian", "main"])
        self.con = collect.connect(collect.db_path())
        self.addCleanup(self.con.close)
        collect.scan(self.con, self.home)

    def test_exit_codes_from_each_output_shape(self):
        rows = dict(self.con.execute("SELECT call_id, exit_code FROM call_view WHERE thread_id='tools-1'"))
        self.assertEqual(rows, {"k1": 0, "k2": 1, "k3": 0, "k4": None, "k5": None, "k6": None, "k6#2": None})

    def test_skills_come_only_from_commands(self):
        rows = self.con.execute("SELECT call_id, skill FROM skill_reads WHERE thread_id='tools-1' ORDER BY call_id, skill").fetchall()
        self.assertEqual(rows, [("k1", "bug-hunt"), ("k4", "grilling"), ("k5", "bug-hunt"), ("k5", "code-analysis"), ("k5", "using-superpowers"), ("k6#2", "regression-tests")])

    def test_every_command_in_a_cell_is_recorded(self):
        rows = self.con.execute("SELECT call_id, cmd FROM calls WHERE call_id LIKE 'k6%' ORDER BY call_id").fetchall()
        self.assertEqual(rows, [("k6", "pytest -q tests/a"), ("k6#2", "cat /home/u/.agents/skills/regression-tests/SKILL.md")])
        self.assertEqual(collect.cell_commands({"type": "function_call", "call_id": "f", "arguments": '{"cmd": "ls"}'}), [])

    def test_guardian_outcomes_only_from_guardian_threads(self):
        self.assertEqual(self.con.execute("SELECT thread_id, outcome FROM guardian").fetchall(), [("g-1", "allow")])

    def test_output_in_a_later_scan_updates_the_call(self):
        home = home_with(self.root / "late", [])
        f = home / "sessions" / "rollout-late.jsonl"
        lines = (FIX / "tools.jsonl").read_text(encoding="utf-8").splitlines(keepends=True)
        write(f, "".join(lines[:6]))
        con = collect.connect(home / "usage.sqlite3")
        self.addCleanup(con.close)
        collect.scan(con, home)
        self.assertIsNone(con.execute("SELECT exit_code FROM call_view WHERE call_id='k2'").fetchone()[0])
        with open(f, "a", encoding="utf-8", newline="\n") as out:
            out.write(lines[6])
        collect.scan(con, home)
        self.assertEqual(con.execute("SELECT exit_code FROM call_view WHERE call_id='k2'").fetchone()[0], 1)

    def test_exit_code_parsing(self):
        meta = json.dumps({"output": "Process exited with code 3", "metadata": {"exit_code": 0}})
        self.assertEqual(collect.exit_code({"output": meta}), 0)
        self.assertEqual(collect.exit_code({"output": "Script running with cell ID 3\nWall time 31.0 seconds"}), None)
        self.assertEqual(collect.exit_code({"output": "Process exited with code -1"}), -1)
        self.assertEqual(collect.exit_code({"output": None}), None)

    def test_rescan_clears_guardian(self):
        collect.scan(self.con, self.home, rescan=True)
        self.assertEqual(self.con.execute("SELECT COUNT(*) FROM guardian").fetchone()[0], 1)


class Migration(unittest.TestCase):
    def test_old_database_gains_columns_and_table(self):
        db = tree(self) / "usage.sqlite3"
        old = sqlite3.connect(db)
        old.execute("CREATE TABLE calls (thread_id TEXT NOT NULL, call_id TEXT NOT NULL, turn_id TEXT, ts TEXT, cmd TEXT, escalated INTEGER NOT NULL, PRIMARY KEY (thread_id, call_id))")
        old.execute("INSERT INTO calls VALUES ('t', 'c', NULL, NULL, 'git status', 0)")
        old.commit()
        old.close()
        con = collect.connect(db)
        self.addCleanup(con.close)
        self.assertEqual(con.execute("SELECT cmd, exit_code FROM call_view").fetchall(), [("git status", None)])
        self.assertEqual(con.execute("SELECT COUNT(*) FROM guardian").fetchone()[0], 0)
        collect.connect(db).close()

    def test_an_old_turn_usage_view_is_replaced(self):
        db = tree(self) / "usage.sqlite3"
        old = sqlite3.connect(db)
        old.executescript("CREATE TABLE usage (thread_id TEXT NOT NULL, key TEXT NOT NULL, turn_id TEXT, ts TEXT, model TEXT, input INTEGER, cached INTEGER, output INTEGER, reasoning INTEGER, PRIMARY KEY (thread_id, key));"
                          "CREATE VIEW turn_usage AS SELECT u.* FROM usage u WHERE u.key NOT LIKE 'tc:%';"
                          "INSERT INTO usage VALUES ('t', 'tc:3', 'a', NULL, NULL, 5, 0, 1, 0);")
        old.close()
        con = collect.connect(db)
        self.addCleanup(con.close)
        self.assertEqual(con.execute("SELECT SUM(input) FROM turn_usage").fetchone(), (5,))

    def test_older_collectors_can_still_write(self):
        db = tree(self) / "usage.sqlite3"
        collect.connect(db).close()
        old = sqlite3.connect(db)
        self.addCleanup(old.close)
        old.execute("INSERT OR IGNORE INTO calls VALUES (?,?,?,?,?,?)", ("t", "c", None, None, "git status", 0))
        old.execute("INSERT OR IGNORE INTO compactions VALUES (?,?,?)", ("t", 1, None))
        old.execute("INSERT INTO call_results (thread_id, call_id, skill) VALUES (?,?,?) "
                    "ON CONFLICT(thread_id, call_id) DO UPDATE SET skill = excluded.skill", ("t", "c", "bug-hunt"))
        self.assertEqual(old.execute("SELECT cmd, exit_code FROM call_view").fetchall(), [("git status", None)])
