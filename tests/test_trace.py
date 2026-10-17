import contextlib
import io
import json
import subprocess
import unittest
from unittest import mock

from codejig import trace
from codejig.__main__ import build_parser
from tests.util import tree, write


class ReadIds(unittest.TestCase):
    def test_plain_lines(self):
        self.assertEqual(trace.read_ids("#12\n\n  34 Login works\nREQ-56\n12\n"), ["12", "34", "56"])

    def test_json_list(self):
        self.assertEqual(trace.read_ids("[1, \"2\", 1]"), ["1", "2"])

    def test_json_objects(self):
        self.assertEqual(trace.read_ids(json.dumps([{"id": 7, "fields": {}}, {"id": 8}])), ["7", "8"])

    def test_json_work_item_query(self):
        self.assertEqual(trace.read_ids(json.dumps({"workItems": [{"id": 9}, {"id": 10}]})), ["9", "10"])

    def test_json_single_object(self):
        self.assertEqual(trace.read_ids(json.dumps({"id": 11, "fields": {"System.Title": "x"}})), ["11"])

    def test_single_value(self):
        self.assertEqual(trace.read_ids("42\n"), ["42"])
        self.assertEqual(trace.read_ids('"#7"'), ["7"])
        self.assertEqual(trace.read_ids("true"), [])

    def test_bom_is_ignored(self):
        self.assertEqual(trace.read_ids("\ufeff[12]"), ["12"])
        self.assertEqual(trace.read_ids("\ufeff12\n"), ["12"])

    def test_broken_json_is_rejected(self):
        for text in ("[12,", '{"id": ', "[{"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                trace.read_ids(text)

    def test_markdown_lines(self):
        self.assertEqual(trace.read_ids("REQ-12: login\n- REQ-13\n* #14 export\n1. REQ-15.\n2) 16\n"), ["12", "13", "14", "15", "16"])

    def test_a_numbered_title_keeps_the_number_as_the_id(self):
        self.assertEqual(trace.read_ids("123. Fix auth\n7) export works\n1. AUTH-3 login\n2. #44 x\n"), ["123", "7", "AUTH-3", "44"])

    def test_json_without_ids(self):
        self.assertEqual(trace.read_ids(json.dumps({"count": 0})), [])


class TestPaths(unittest.TestCase):
    def test_matches(self):
        for path in ("tests/test_a.py", "pkg/test_b.py", "pkg/b_test.py", "src/__tests__/x.ts", "src/x.test.ts",
                     "src/x.spec.tsx", "web/x.test.mjs", "Api.Tests/LoginTests.cs", "test/Foo.cs", "Svc/FooTest.cs",
                     "specs/login.js"):
            with self.subTest(path=path):
                self.assertTrue(trace.TEST_PATH.search(path))

    def test_non_matches(self):
        for path in ("src/app.py", "src/latest.ts", "src/contest.py", "Api/Login.cs", "docs/testing.md", "src/attest/x.py"):
            with self.subTest(path=path):
                self.assertFalse(trace.TEST_PATH.search(path))


class Trace(unittest.TestCase):
    def setUp(self):
        self.root = tree(self)
        write(self.root / "tests" / "test_login.py", "def test_login():\n    pass  # REQ-12\n")
        write(self.root / "Api.Tests" / "LoginTests.cs", '[Trait("Requirement", "req-123")]\npublic void Login() {}\n')
        write(self.root / "src" / "app.py", "REQ-77 is mentioned in code, not in a test\n")
        write(self.root / "web" / "x.test.ts", "it('REQ-900 and REQ-12. done', () => {})\n")

    def test_coverage_missing_unknown(self):
        result = trace.trace(self.root, ["12", "123", "77", "1"])
        self.assertEqual(result.covered["12"], ["tests/test_login.py", "web/x.test.ts"])
        self.assertEqual(result.covered["123"], ["Api.Tests/LoginTests.cs"])
        self.assertEqual(result.missing, ["77", "1"])
        self.assertEqual(result.unknown, {"900": ["web/x.test.ts"]})

    def test_ids_differing_only_in_case_are_one_requirement(self):
        write(self.root / "tests" / "test_auth.py", "# REQ-auth-1\n")
        result = trace.trace(self.root, ["AUTH-1", "auth-1"])
        self.assertEqual(result.covered, {"AUTH-1": ["tests/test_auth.py"]})
        self.assertEqual(result.missing, [])

    def test_custom_pattern(self):
        write(self.root / "tests" / "test_wi.py", "# AB#4321\n")
        result = trace.trace(self.root, ["4321"], r"AB#{id}")
        self.assertEqual(result.covered, {"4321": ["tests/test_wi.py"]})
        self.assertEqual(result.unknown, {})

    def test_pattern_is_literal_text(self):
        write(self.root / "tests" / "test_lit.py", "# (AB)#4321 and [REQ:7]\n")
        self.assertEqual(trace.trace(self.root, ["4321"], "(AB)#{id}").covered, {"4321": ["tests/test_lit.py"]})
        self.assertEqual(trace.trace(self.root, ["7"], "[REQ:{id}]").covered, {"7": ["tests/test_lit.py"]})
        self.assertEqual(trace.trace(self.root, ["4321"], "(AB)#{id}").unknown, {})

    def test_pattern_needs_placeholder(self):
        with self.assertRaisesRegex(ValueError, "{id}"):
            trace.trace(self.root, ["1"], "REQ")

    def test_git_repo_uses_tracked_files_only(self):
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True)
        write(self.root / ".gitignore", "ignored/\n")
        write(self.root / "ignored" / "tests" / "test_x.py", "REQ-77\n")
        self.assertEqual(trace.trace(self.root, ["77"]).missing, ["77"])

    def test_render_escapes_control_characters(self):
        result = trace.Result({"1": ["tests/a\x1b[2J.py"]}, ["evil\x1b]0;x\x07"], {"9\x1b": ["tests/b.py"]})
        text = "\n".join(trace.render(result))
        self.assertNotIn("\x1b", text)
        self.assertNotIn("\x07", text)
        self.assertIn("evil\\x1b]0;x\\x07", text)

    def test_render(self):
        text = "\n".join(trace.render(trace.trace(self.root, ["12", "1"])))
        self.assertIn("1 of 2 requirements have tests", text)
        self.assertIn("missing: 1", text)
        self.assertIn("unknown in tests: 123 (Api.Tests/LoginTests.cs)", text)


class Cli(unittest.TestCase):
    def setUp(self):
        self.root = tree(self)
        write(self.root / "tests" / "test_a.py", "REQ-1\n")
        self.out = self.enterContext(contextlib.redirect_stdout(io.StringIO()))
        self.err = self.enterContext(contextlib.redirect_stderr(io.StringIO()))

    def run_cli(self, *argv):
        args = build_parser().parse_args(["trace", "--path", str(self.root), *argv])
        return trace.cli(args)

    def test_all_covered(self):
        write(self.root / "ids.txt", "1\n")
        self.assertEqual(self.run_cli(str(self.root / "ids.txt")), 0)

    def test_missing_exits_one(self):
        write(self.root / "ids.txt", "1\n2\n")
        self.assertEqual(self.run_cli(str(self.root / "ids.txt")), 1)

    def test_stdin(self):
        with mock.patch("sys.stdin", io.StringIO("[1]")):
            self.assertEqual(self.run_cli("-"), 0)

    def test_json(self):
        write(self.root / "ids.txt", "1\n2\n")
        self.run_cli(str(self.root / "ids.txt"), "--json")
        self.assertEqual(json.loads(self.out.getvalue())["missing"], ["2"])

    def test_no_ids(self):
        write(self.root / "ids.txt", "\n")
        self.assertEqual(self.run_cli(str(self.root / "ids.txt")), 2)

    def test_unreadable_file(self):
        self.assertEqual(self.run_cli(str(self.root / "nope.txt")), 2)

    def test_utf16_file(self):
        (self.root / "ids.txt").write_bytes("1\r\n".encode("utf-16"))
        self.assertEqual(self.run_cli(str(self.root / "ids.txt")), 0)

    def test_undecodable_file_exits_two(self):
        (self.root / "ids.txt").write_bytes(b"\x81\x8d 1\n")
        self.assertEqual(self.run_cli(str(self.root / "ids.txt")), 2)

    def test_broken_json_exits_two(self):
        write(self.root / "ids.json", "[1,")
        self.assertEqual(self.run_cli(str(self.root / "ids.json")), 2)

    def test_narrow_console_encoding_does_not_crash(self):
        write(self.root / "tests" / "test_łódź.py", "REQ-1\n")
        write(self.root / "ids.txt", "1\n")
        narrow = io.TextIOWrapper(io.BytesIO(), encoding="cp1252")
        with contextlib.redirect_stdout(narrow):
            self.assertEqual(self.run_cli(str(self.root / "ids.txt")), 0)
            self.assertEqual(self.run_cli(str(self.root / "ids.txt"), "--json"), 0)

    def test_bad_pattern(self):
        write(self.root / "ids.txt", "1\n")
        self.assertEqual(self.run_cli(str(self.root / "ids.txt"), "--pattern", "("), 2)


class Parser(unittest.TestCase):
    def test_analyze_flags(self):
        a = build_parser().parse_args(["analyze", "src", "--base", "main", "--top", "5", "--json", "--timeout", "30"])
        self.assertEqual((a.cmd, a.path, a.base, a.top, a.json, a.timeout), ("analyze", "src", "main", 5, True, 30))

    def test_analyze_defaults(self):
        a = build_parser().parse_args(["analyze"])
        self.assertEqual((a.path, a.base, a.top, a.json, a.timeout), (".", None, 10, False, 600))

    def test_trace_defaults(self):
        a = build_parser().parse_args(["trace", "ids.txt"])
        self.assertEqual((a.ids, a.path, a.pattern, a.json), ("ids.txt", ".", "REQ-{id}", False))

