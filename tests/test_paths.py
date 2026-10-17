import types
import unittest
from pathlib import Path
from unittest import mock

from codejig import install, paths
from codejig.overlay import Overlay, load
from tests.util import tree


class StateDir(unittest.TestCase):
    def test_state_dir_follows_xdg_data_home(self):
        fake = types.SimpleNamespace(name="posix", environ={"XDG_DATA_HOME": "/x/data"})
        with mock.patch.object(paths, "os", fake):
            self.assertEqual(paths.state_dir(), Path("/x/data") / "code-jig")

    def test_state_dir_defaults_under_home(self):
        fake = types.SimpleNamespace(name="posix", environ={})
        with mock.patch.object(paths, "os", fake):
            self.assertEqual(paths.state_dir(), Path.home() / ".local" / "share" / "code-jig")

    def test_state_dir_uses_localappdata_on_windows(self):
        fake = types.SimpleNamespace(name="nt", environ={"LOCALAPPDATA": "/win/a b/Local", "XDG_DATA_HOME": "/ignored"})
        with mock.patch.object(paths, "os", fake):
            self.assertEqual(paths.state_dir(), Path("/win/a b/Local") / "code-jig")

    def test_state_dir_without_localappdata_falls_back_under_home(self):
        fake = types.SimpleNamespace(name="nt", environ={})
        with mock.patch.object(paths, "os", fake):
            self.assertEqual(paths.state_dir(), Path.home() / "AppData" / "Local" / "code-jig")

    def test_install_puts_lib_and_git_hooks_in_the_state_dir(self):
        root = tree(self)
        home = root / "codex home"
        ov = load(paths.CORE / "overlay-examples" / "personal")
        outputs = {p for p, _ in install.plan(paths.CORE, ov, home)}
        state = paths.state_dir()
        self.assertTrue(any(p.is_relative_to(state / "lib" / "codejig") for p in outputs))
        self.assertFalse(any(p.is_relative_to(home / "code-jig") for p in outputs))
        with mock.patch.object(install, "_git_config", return_value="") as git:
            install.git_gate(Overlay(root / "ov", "v1", True, git_gate=True, prose_gate=True))
        self.assertTrue((state / "git-hooks" / "pre-commit").is_file())
        git.assert_called_with("core.hooksPath", str(state / "git-hooks"))
