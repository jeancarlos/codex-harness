import contextlib
import io
import re
import subprocess
import unittest
from pathlib import Path

from codejig import VERSION
from codejig.__main__ import build_parser

ROOT = Path(__file__).resolve().parent.parent
OLD_NAMES = (("codex-" "harness"), ("codex" "harness"), ("CODEX_" "HARNESS_"), ("-m " "harness"), ('"-m", "' 'harness"'), ("from " "harness"))
OLD_NAMES_ALLOWED = ("tests/fixtures/", "tests/test_golden.py")
OLD_ADR = re.compile(r"docs/adr/00(0[1-9]|1\d|2[0-7])-[^/]*\.md")


class Cli(unittest.TestCase):
    def test_version_is_semver_or_a_release_candidate(self):
        self.assertRegex(VERSION, r"^\d+\.\d+\.\d+(-rc\d+)?$")

    def test_doctor_flags_parse(self):
        a = build_parser().parse_args(["--overlay", "x", "doctor", "--codex-bin", "y", "--check-binary"])
        self.assertEqual((a.cmd, a.overlay, a.codex_bin, a.check_binary), ("doctor", "x", "y", True))

    def test_version_flag_prints_the_package_version(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as done:
            build_parser().parse_args(["--version"])
        self.assertEqual((done.exception.code, out.getvalue()), (0, f"code-jig {VERSION}\n"))

    def test_command_required(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            build_parser().parse_args([])

    def test_no_old_names_outside_fixtures(self):
        files = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True).stdout
        hits = []
        for rel in files.decode("utf-8").split("\0"):
            path = ROOT / rel
            if not rel or rel.startswith(OLD_NAMES_ALLOWED) or OLD_ADR.fullmatch(rel) or not path.is_file():
                continue
            text = path.read_bytes().decode("utf-8", "ignore")
            hits += [f"{rel}: {name}" for name in OLD_NAMES if name in text]
        self.assertEqual(hits, [])
