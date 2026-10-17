import contextlib
import hashlib
import io
import subprocess
import types
import unittest
from unittest import mock

from codejig.doctor import check_core, check_global, cli, package_digest
from codejig.install import render_agents, stamp
from codejig.overlay import Overlay
from tests.util import tree, write


def git(repo, *args):
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *args],
                   check=True, capture_output=True)


class Core(unittest.TestCase):
    def setUp(self):
        self.core = tree(self) / "core"
        write(self.core / "f.txt", "a")
        git(self.core, "init", "-q")
        git(self.core, "add", "-A")
        git(self.core, "commit", "-q", "-m", "c")
        git(self.core, "tag", "v0.1.0")

    def levels(self, ov):
        return {name: level for level, name, _ in check_core(self.core, ov)}

    def test_pinned_clean_readonly_ok(self):
        self.assertEqual(self.levels(Overlay(self.core, "v0.1.0", False)), {"core pin": "ok", "core clean": "ok"})

    def test_dirty_readonly_fails(self):
        write(self.core / "f.txt", "changed")
        self.assertEqual(self.levels(Overlay(self.core, "v0.1.0", False))["core clean"], "FAIL")

    def test_dirty_writable_is_fine(self):
        write(self.core / "f.txt", "changed")
        self.assertEqual(self.levels(Overlay(self.core, "v0.1.0", True))["core clean"], "ok")

    def test_wrong_pin_readonly_fails_writable_warns(self):
        self.assertEqual(self.levels(Overlay(self.core, "v9.9.9", False))["core pin"], "FAIL")
        self.assertEqual(self.levels(Overlay(self.core, "v9.9.9", True))["core pin"], "warn")


class NotGit(unittest.TestCase):
    def setUp(self):
        self.core = tree(self) / "core"
        write(self.core / "f.txt", "a")

    def test_non_git_core_fails_clean_both_modes(self):
        for writable in (False, True):
            levels = {name: level for level, name, _ in check_core(self.core, Overlay(self.core, "v0.1.0", writable))}
            self.assertEqual(levels["core clean"], "FAIL")

    def test_missing_git_fails_without_crashing(self):
        with mock.patch("codejig.doctor.subprocess.run", side_effect=FileNotFoundError("git")):
            checks = check_core(self.core, Overlay(self.core, "v0.1.0", False))
        self.assertEqual({name: level for level, name, _ in checks}["core clean"], "FAIL")


class GitFailure(unittest.TestCase):
    def setUp(self):
        self.core = tree(self) / "core"
        write(self.core / "f.txt", "a")

    def test_stderr_in_detail_and_pin_not_untagged(self):
        fail = mock.Mock(returncode=128, stdout="", stderr="fatal: detected dubious ownership\nmore\n")
        with mock.patch("codejig.doctor.subprocess.run", return_value=fail):
            pin, clean = check_core(self.core, Overlay(self.core, "v0.1.0", False))
        self.assertIn("dubious ownership", clean[2])
        self.assertNotIn("more", clean[2])
        self.assertNotIn("untagged", pin[2])
        self.assertIn("cannot be checked", pin[2])


class GlobalAgents(unittest.TestCase):
    def setUp(self):
        root = tree(self)
        self.core = root / "core"
        write(self.core / "core" / "AGENTS.core.md", "core rules\n")
        self.ovdir = root / "ov"
        self.ovdir.mkdir()
        self.ov = Overlay(self.ovdir, "v0.1.0", True)
        self.home = root / "home"
        self.home.mkdir()

    def level(self):
        [(level, name, _)] = check_global(self.home, self.core, self.ov)
        self.assertEqual(name, "global AGENTS.md")
        return level

    def test_ok(self):
        write(self.home / "AGENTS.md", render_agents(self.core, self.ovdir))
        self.assertEqual(self.level(), "ok")

    def test_missing_warns(self):
        self.assertEqual(self.level(), "warn")

    def test_stale_warns(self):
        write(self.home / "AGENTS.md", stamp("old\n"))
        self.assertEqual(self.level(), "warn")

    def test_override_fails(self):
        write(self.home / "AGENTS.md", render_agents(self.core, self.ovdir))
        write(self.home / "AGENTS.override.md", "x\n")
        self.assertEqual(self.level(), "FAIL")

    def test_empty_override_ignored(self):
        write(self.home / "AGENTS.md", render_agents(self.core, self.ovdir))
        write(self.home / "AGENTS.override.md", "")
        self.assertEqual(self.level(), "ok")


class Release(unittest.TestCase):
    def setUp(self):
        self.code = tree(self) / "codejig"
        self.core = self.code / "_core"
        write(self.code / "x.py", "a\n")
        write(self.core / "core" / "AGENTS.core.md", "rules\n")
        write(self.core / "VERSION", f"v0.1.0\n{package_digest(self.code)}\n")

    def levels(self, ov):
        return {name: level for level, name, _ in check_core(self.core, ov)}

    def test_matching_release_is_ok(self):
        self.assertEqual(self.levels(Overlay(self.core, "v0.1.0", False)), {"core pin": "ok", "core clean": "ok"})

    def test_edited_code_or_data_fails_readonly_ok_writable(self):
        for rel in ("x.py", "_core/core/AGENTS.core.md"):
            with self.subTest(rel=rel):
                original = (self.code / rel).read_text(encoding="utf-8")
                write(self.code / rel, "tampered\n")
                self.assertEqual(self.levels(Overlay(self.core, "v0.1.0", False))["core clean"], "FAIL")
                self.assertEqual(self.levels(Overlay(self.core, "v0.1.0", True))["core clean"], "ok")
                write(self.code / rel, original)
                self.assertEqual(self.levels(Overlay(self.core, "v0.1.0", False))["core clean"], "ok")

    def test_added_module_fails_readonly(self):
        write(self.code / "extra.py", "import os\n")
        self.assertEqual(self.levels(Overlay(self.core, "v0.1.0", False))["core clean"], "FAIL")

    def test_wrong_pin_readonly_fails_writable_warns(self):
        self.assertEqual(self.levels(Overlay(self.core, "v9.9.9", False))["core pin"], "FAIL")
        self.assertEqual(self.levels(Overlay(self.core, "v9.9.9", True))["core pin"], "warn")

    def test_pycache_and_version_are_not_digested(self):
        write(self.code / "__pycache__" / "x.cpython-313.pyc", "bytecode")
        write(self.core / "core" / "__pycache__" / "y.pyc", "bytecode")
        self.assertEqual(self.levels(Overlay(self.core, "v0.1.0", False)), {"core pin": "ok", "core clean": "ok"})

    def test_parent_git_repo_does_not_count(self):
        git(self.code.parent, "init", "-q")
        self.assertEqual(self.levels(Overlay(self.core, "v0.1.0", False)), {"core pin": "ok", "core clean": "ok"})

    def test_unreadable_version_fails(self):
        with mock.patch("pathlib.Path.read_text", side_effect=PermissionError("denied")):
            out = check_core(self.core, Overlay(self.core, "v0.1.0", False))
        self.assertEqual(out[0][0], "FAIL")
        self.assertIn("cannot read VERSION", out[0][2])

    def test_garbled_version_file_fails_readonly(self):
        write(self.core / "VERSION", "nonsense")
        levels = self.levels(Overlay(self.core, "v0.1.0", False))
        self.assertEqual((levels["core pin"], levels["core clean"]), ("FAIL", "FAIL"))


class PackageDigest(unittest.TestCase):
    def test_order_is_by_posix_string_with_length_framing(self):
        root = tree(self)
        write(root / "a-b" / "x", "1")
        write(root / "a" / "b", "2")
        h = hashlib.sha256()
        for rel in sorted(["a-b/x", "a/b"]):
            data = (root / rel).read_bytes()
            h.update(len(rel).to_bytes(8, "big") + rel.encode() + len(data).to_bytes(8, "big") + data)
        self.assertEqual(package_digest(root), h.hexdigest())

    def test_one_file_cannot_pose_as_two(self):
        one, two = tree(self), tree(self)
        write(one / "a", "x\0b\0y")
        write(two / "a", "x")
        write(two / "b", "y")
        self.assertNotEqual(package_digest(one), package_digest(two))


class Requirements(unittest.TestCase):
    def test_doctor_fails_without_git(self):
        out = io.StringIO()
        args = types.SimpleNamespace(codex_bin=None, codex_home=str(tree(self) / "h"), check_binary=True, overlay=None, live=False)
        with mock.patch("shutil.which", return_value=None), contextlib.redirect_stdout(out):
            code = cli(args)
        self.assertEqual(code, 1)
        self.assertIn("FAIL requirements: git not found", out.getvalue())
