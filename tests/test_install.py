import contextlib
import io
import json
import os
import types
import unittest
from unittest import mock

from codejig import install
from codejig.install import (
    MARKER_PREFIX,
    cli,
    is_generated,
    render_agents,
    run,
    stamp,
    write_generated,
)
from codejig.overlay import Overlay
from tests.util import tree, write


class Render(unittest.TestCase):
    def setUp(self):
        self.root = tree(self)
        self.core = self.root / "core-repo"
        write(self.core / "core" / "AGENTS.core.md", "core rules\n")
        self.ov = self.root / "overlay"

    def test_core_then_overlay(self):
        write(self.ov / "AGENTS.overlay.md", "overlay rules\n")
        text = render_agents(self.core, self.ov)
        self.assertTrue(text.startswith(MARKER_PREFIX))
        self.assertLess(text.index("core rules"), text.index("overlay rules"))

    def test_overlay_file_optional(self):
        self.ov.mkdir(parents=True)
        self.assertEqual(render_agents(self.core, self.ov), stamp("core rules\n"))


class LauncherLine(unittest.TestCase):
    def setUp(self):
        self.root = tree(self)
        self.core = self.root / "core-repo"
        write(self.core / "core" / "AGENTS.core.md", "core rules\n")
        self.ov = self.root / "overlay"
        self.ov.mkdir()
        self.launcher = self.root / "off path" / "code-jig"

    def render(self, path_dirs):
        env = {"CODE_JIG_LAUNCHER": str(self.launcher), "PATH": os.pathsep.join(map(str, path_dirs))}
        with mock.patch.dict(os.environ, env):
            return render_agents(self.core, self.ov)

    def test_agents_md_names_the_launcher_when_off_path(self):
        text = self.render([self.root])
        self.assertIn(f"On this machine `code-jig` is not on PATH; run it as `{self.launcher}`.", text)

    def test_cjig_and_code_jig_write_the_same_agents_md(self):
        code_jig = self.render([self.root])
        self.launcher = self.launcher.with_name("cjig")
        self.assertEqual(self.render([self.root]), code_jig)
        self.launcher = self.launcher.with_name("cjig.cmd")
        self.assertIn(f"run it as `{self.launcher.with_name('code-jig.cmd')}`.", self.render([self.root]))

    def test_agents_md_unchanged_when_launcher_on_path(self):
        self.assertEqual(self.render([self.root, self.launcher.parent]), stamp("core rules\n"))


class WriteGenerated(unittest.TestCase):
    def setUp(self):
        self.home = tree(self) / "codex home"
        self.target = self.home / "AGENTS.md"

    def test_creates_new(self):
        self.assertIsNone(write_generated(self.target, stamp("x\n")))
        self.assertEqual(self.target.read_text(encoding="utf-8"), stamp("x\n"))

    def test_overwrites_own_file_without_backup(self):
        write(self.target, stamp("old\n"))
        self.assertIsNone(write_generated(self.target, stamp("new\n")))
        self.assertEqual(list(self.home.glob("AGENTS.md.bak-*")), [])

    def test_backs_up_user_file(self):
        write(self.target, "my own rules\n")
        backup = write_generated(self.target, stamp("new\n"))
        self.assertEqual(backup.read_text(encoding="utf-8"), "my own rules\n")
        self.assertEqual(self.target.read_text(encoding="utf-8"), stamp("new\n"))

    def test_bom_on_own_file_is_still_own(self):
        self.home.mkdir(parents=True)
        self.target.write_bytes(b"\xef\xbb\xbf" + (stamp("old\n")).encode())
        self.assertIsNone(write_generated(self.target, stamp("new\n")))

    def test_edited_generated_file_backed_up(self):
        write(self.target, stamp("old\n"))
        original = self.target.read_bytes()
        self.target.write_bytes(original + b"my edit\n")
        backup = write_generated(self.target, stamp("new\n"))
        self.assertEqual(backup.read_bytes(), original + b"my edit\n")

    def test_failed_write_keeps_user_file(self):
        write(self.target, "mine\n")
        with mock.patch("codejig.atomicwrite.os.fsync", side_effect=OSError("disk full")), self.assertRaises(OSError):
            write_generated(self.target, stamp("new\n"))
        self.assertEqual(self.target.read_text(encoding="utf-8"), "mine\n")

    def test_non_utf8_user_file_backed_up(self):
        self.home.mkdir(parents=True)
        self.target.write_bytes(b"regras \xe7\xe3o\n")
        backup = write_generated(self.target, stamp("new\n"))
        self.assertEqual(backup.read_bytes(), b"regras \xe7\xe3o\n")


    def test_same_second_backup_is_not_overwritten(self):
        self.home.mkdir(parents=True)
        stamped = self.home / "AGENTS.md.bak-20260101-000000"
        stamped.write_bytes(b"first backup\n")
        write(self.target, "second user file\n")
        with mock.patch.object(install.time, "strftime", return_value="20260101-000000"):
            backup = write_generated(self.target, stamp("new\n"))
        self.assertEqual(stamped.read_bytes(), b"first backup\n")
        self.assertEqual(backup.read_bytes(), b"second user file\n")
        self.assertNotEqual(backup, stamped)


class Cli(unittest.TestCase):
    def test_malformed_overlay_exits_2_and_writes_nothing(self):
        root = tree(self)
        ov = root / "overlay"
        write(ov / "overlay.toml", "core_version = \n")
        home = root / "codex home"
        home.mkdir(parents=True)
        err = io.StringIO()
        args = types.SimpleNamespace(overlay=str(ov), codex_home=str(home))
        with contextlib.redirect_stderr(err):
            code = cli(args)
        self.assertEqual(code, 2)
        message = err.getvalue()
        self.assertEqual(message.count("\n"), 1)
        self.assertIn("overlay.toml", message)
        self.assertFalse((home / "AGENTS.md").exists())

    def test_install_cli_refuses_without_git(self):
        root = tree(self)
        home = root / "codex home"
        err = io.StringIO()
        args = types.SimpleNamespace(overlay=str(root / "overlay"), codex_home=str(home))
        with mock.patch("shutil.which", return_value=None), contextlib.redirect_stderr(err):
            code = cli(args)
        self.assertEqual(code, 2)
        self.assertIn("missing requirement: git (", err.getvalue())
        self.assertFalse(home.exists())

    def test_bad_profiles_exits_2(self):
        root = tree(self)
        core, ov = tiered(root)
        write(ov.root / "overlay.toml", 'core_version = "v0.2.0"\ncore_writable = true\n')
        write(ov.root / "profiles.toml", "default = \n")
        home = root / "codex home"
        err = io.StringIO()
        args = types.SimpleNamespace(overlay=str(ov.root), codex_home=str(home))
        with mock.patch.object(install.paths, "CORE", core), contextlib.redirect_stderr(err):
            code = cli(args)
        self.assertEqual(code, 2)
        self.assertIn("profiles.toml", err.getvalue())
        self.assertFalse(home.exists())



def tiered(root):
    core = root / "core-repo"
    write(core / "core" / "AGENTS.core.md", "core rules\n")
    write(core / "core" / "tiers.toml", 'default = "medium"\nreview = "high"\n')
    ov = root / "overlay"
    write(ov / "profiles.toml", 'default = "m-a"\nreview = "n-b"\n')
    return core, Overlay(ov, "v0.2.0", True)


class Run(unittest.TestCase):
    def test_override_warns(self):
        root = tree(self)
        core, ov = tiered(root)
        home = root / "codex home"
        write(home / "AGENTS.override.md", "x\n")
        lines = run(core, ov, home)
        self.assertTrue(any("AGENTS.override.md" in line for line in lines))

    def test_empty_override_is_silent(self):
        root = tree(self)
        core, ov = tiered(root)
        home = root / "codex home"
        write(home / "AGENTS.override.md", "")
        lines = run(core, ov, home)
        self.assertFalse(any("override" in line for line in lines))

    def test_end_to_end(self):
        root = tree(self)
        core, ov = tiered(root)
        home = root / "codex home"
        lines = run(core, ov, home)
        self.assertIn("core rules", (home / "AGENTS.md").read_text(encoding="utf-8"))
        self.assertTrue(any("AGENTS.md" in line for line in lines))


    def test_a_relative_codex_home_is_absolute_in_the_hook_commands(self):
        root = tree(self)
        core, ov = tiered(root)
        cwd = os.getcwd()
        os.chdir(root)
        self.addCleanup(os.chdir, cwd)
        [hooks] = [text for target, text in install.plan(core, ov, install.Path("rel home")) if target.name == "hooks.json"]
        commands = [h["command"] for group in json.loads(hooks)["hooks"]["Stop"] for h in group["hooks"]]
        self.assertTrue(any(str(root / "rel home") in c for c in commands), commands)


class SymlinkedConfig(unittest.TestCase):
    def setUp(self):
        self.root = tree(self)
        self.home = self.root / "codex home"
        self.home.mkdir()
        self.dotfiles = self.root / "dotfiles"

    def link(self, name, text):
        real = write(self.dotfiles / name, text)
        try:
            (self.home / name).symlink_to(real)
        except OSError:
            self.skipTest("no symlinks here")
        return real

    def test_a_generated_file_is_written_through_its_link(self):
        real = self.link("AGENTS.md", "my own rules\n")
        backup = write_generated(self.home / "AGENTS.md", stamp("core\n"))
        self.assertTrue((self.home / "AGENTS.md").is_symlink())
        self.assertIn("core", real.read_text(encoding="utf-8"))
        self.assertEqual(backup.read_text(encoding="utf-8"), "my own rules\n")

    def test_merged_settings_are_written_through_their_link(self):
        real = self.link("settings.json", "{}\n")
        install.write_merged(self.home / "settings.json", '{"hooks": {}}\n')
        self.assertTrue((self.home / "settings.json").is_symlink())
        self.assertEqual(real.read_text(encoding="utf-8"), '{"hooks": {}}\n')

    def test_a_dangling_link_is_refused(self):
        try:
            (self.home / "hooks.json").symlink_to(self.dotfiles / "missing.json")
        except OSError:
            self.skipTest("no symlinks here")
        with self.assertRaisesRegex(ValueError, "missing.json"):
            install.write_merged(self.home / "hooks.json", "{}\n")


class Profiles(unittest.TestCase):
    def test_writes_one_profile_per_tier(self):
        root = tree(self)
        core, ov = tiered(root)
        home = root / "codex home"
        run(core, ov, home)
        self.assertTrue(is_generated(home / "default.config.toml"))
        self.assertIn('model = "n-b"', (home / "review.config.toml").read_text(encoding="utf-8"))

    def test_user_profile_with_tier_name_is_backed_up(self):
        root = tree(self)
        core, ov = tiered(root)
        home = root / "codex home"
        write(home / "default.config.toml", 'model = "mine"\n')
        lines = run(core, ov, home)
        [backup] = home.glob("default.config.toml.bak-*")
        self.assertEqual(backup.read_text(encoding="utf-8"), 'model = "mine"\n')
        self.assertTrue(any(str(backup) in line for line in lines))

    def test_bad_profiles_writes_nothing(self):
        root = tree(self)
        core, ov = tiered(root)
        write(ov.root / "profiles.toml", 'default = "m-a"\n')
        home = root / "codex home"
        with self.assertRaises(ValueError):
            run(core, ov, home)
        self.assertFalse(home.exists())

    def test_user_config_toml_untouched(self):
        root = tree(self)
        core, ov = tiered(root)
        home = root / "codex home"
        write(home / "config.toml", 'model = "mine"\n')
        run(core, ov, home)
        self.assertEqual((home / "config.toml").read_text(encoding="utf-8"), 'model = "mine"\n')


class HashStyle(unittest.TestCase):
    def setUp(self):
        self.home = tree(self) / "codex home"
        self.target = self.home / "default.config.toml"

    def test_hash_marker_is_a_toml_comment(self):
        import tomllib
        text = stamp('model = "x"\n', "hash")
        self.assertTrue(text.startswith("# generated by code-jig sha256:"))
        self.assertEqual(tomllib.loads(text), {"model": "x"})

    def test_hash_file_is_recognised_and_rewritten_without_backup(self):
        write(self.target, stamp('model = "a"\n', "hash"))
        self.assertTrue(is_generated(self.target))
        self.assertIsNone(write_generated(self.target, stamp('model = "b"\n', "hash")))

    def test_edited_hash_file_is_not_generated(self):
        write(self.target, stamp('model = "a"\n', "hash") + "x = 1\n")
        self.assertFalse(is_generated(self.target))

    def test_md_marker_unchanged(self):
        self.assertTrue(stamp("x\n").startswith(MARKER_PREFIX))
        self.assertTrue(stamp("x\n").splitlines()[0].endswith(" -->"))

    def test_marker_only_line_is_not_generated(self):
        write(self.target, "# generated by code-jig sha256: : edit the core or the overlay, not this file\n")
        self.assertFalse(is_generated(self.target))


class AtomicInstall(unittest.TestCase):
    def test_writers_preserve_existing_temporary_file(self):
        root = tree(self)
        for writer in (write_generated, install.write_merged):
            with self.subTest(writer=writer.__name__):
                path = write(root / "config.toml", "old\n")
                temporary = write(root / "config.toml.tmp", "unrelated data\n")
                writer(path, "new\n")
                self.assertTrue(temporary.exists())
                self.assertEqual(temporary.read_text(encoding="utf-8"), "unrelated data\n")
                self.assertEqual(path.read_text(encoding="utf-8"), "new\n")

    def test_failed_replacement_keeps_original_file(self):
        root = tree(self)
        path = write(root / "config.toml", "old\n")
        replace = os.replace

        def fail_target(source, target):
            if target == path:
                raise OSError("replacement failed")
            return replace(source, target)

        with mock.patch("os.replace", side_effect=fail_target), self.assertRaises(OSError):
            write_generated(path, "new\n")
        self.assertTrue(path.exists())
        self.assertEqual(path.read_text(encoding="utf-8"), "old\n")

    def test_writers_do_not_follow_dangling_backup_symlinks(self):
        root = tree(self)
        for writer in (write_generated, install.write_merged):
            with self.subTest(writer=writer.__name__):
                path = write(root / writer.__name__, "old\n")
                victim = root / (writer.__name__ + "-victim")
                planted = path.with_name(path.name + ".bak-20260101-000000")
                planted.symlink_to(victim)
                with mock.patch.object(install.time, "strftime", return_value="20260101-000000"):
                    backup = writer(path, "new\n")
                self.assertFalse(victim.exists())
                self.assertTrue(planted.is_symlink())
                self.assertNotEqual(backup, planted)
                self.assertEqual(backup.read_text(encoding="utf-8"), "old\n")
