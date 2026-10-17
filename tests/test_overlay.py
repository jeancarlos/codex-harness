import contextlib
import io
import os
import types
import unittest
from pathlib import Path
from unittest import mock

from codejig import VERSION, overlay, paths, tiers
from codejig.overlay import load
from tests.util import tree, write


class LoadOverlay(unittest.TestCase):
    def setUp(self):
        self.root = tree(self)

    def test_reads_fields(self):
        write(self.root / "overlay.toml", 'core_version = "v0.1.0"\ncore_writable = false\n')
        o = load(self.root)
        self.assertEqual((o.root, o.core_version, o.core_writable), (self.root, "v0.1.0", False))

    def test_missing_key_named(self):
        write(self.root / "overlay.toml", 'core_version = "v0.1.0"\n')
        with self.assertRaisesRegex(ValueError, "core_writable"):
            load(self.root)

    def test_non_bool_writable(self):
        write(self.root / "overlay.toml", 'core_version = "v0.1.0"\ncore_writable = "no"\n')
        with self.assertRaisesRegex(ValueError, "true or false"):
            load(self.root)

    def test_compact_limit_defaults_to_none(self):
        write(self.root / "overlay.toml", 'core_version = "v0.1.0"\ncore_writable = true\n')
        self.assertIsNone(load(self.root).auto_compact_token_limit)

    def test_compact_limit_read(self):
        write(self.root / "overlay.toml", 'core_version = "v0.1.0"\ncore_writable = true\nauto_compact_token_limit = 150000\n')
        self.assertEqual(load(self.root).auto_compact_token_limit, 150000)

    def test_compact_limit_must_be_a_positive_integer(self):
        for bad in ("0", "-5", '"150000"', "true", "1.5"):
            write(self.root / "overlay.toml", f'core_version = "v0.1.0"\ncore_writable = true\nauto_compact_token_limit = {bad}\n')
            with self.subTest(bad=bad), self.assertRaisesRegex(ValueError, "auto_compact_token_limit must be a positive integer"):
                load(self.root)

    def test_malformed_toml_names_file(self):
        write(self.root / "overlay.toml", "core_version = \n")
        with self.assertRaisesRegex(ValueError, "overlay.toml"):
            load(self.root)


class Encodings(unittest.TestCase):
    def test_bom_toml_loads(self):
        root = tree(self)
        (root / "overlay.toml").write_bytes(b"\xef\xbb\xbf" + b'core_version = "v0.1.0"\ncore_writable = true\n')
        self.assertEqual(load(root).core_version, "v0.1.0")

    def test_utf16_toml_names_file(self):
        root = tree(self)
        (root / "overlay.toml").write_bytes('core_version = "v0.1.0"\n'.encode("utf-16"))
        with self.assertRaisesRegex(ValueError, "overlay.toml"):
            load(root)


class OverlayDir(unittest.TestCase):
    def test_unset_exits_with_hint(self):
        missing = tree(self) / "none"
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(paths, "default_overlay", return_value=missing), \
                self.assertRaisesRegex(SystemExit, "init-overlay.*CODE_JIG_OVERLAY"):
            paths.overlay_dir(None)

    def test_default_folder_used_when_it_has_an_overlay(self):
        root = tree(self)
        write(root / "overlay.toml", 'core_version = "v0.1.0"\ncore_writable = true\n')
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(paths, "default_overlay", return_value=root):
            self.assertEqual(paths.overlay_dir(None), root)

    def test_env_beats_the_default_folder(self):
        root, other = tree(self), tree(self)
        for d in (root, other):
            write(d / "overlay.toml", 'core_version = "v0.1.0"\ncore_writable = true\n')
        with mock.patch.dict(os.environ, {"CODE_JIG_OVERLAY": str(other)}), \
                mock.patch.object(paths, "default_overlay", return_value=root):
            self.assertEqual(paths.overlay_dir(None), other)

    def test_default_folder_is_under_home(self):
        self.assertEqual(paths.default_overlay(), Path.home() / "code-jig-overlay")

    def test_missing_toml_exits(self):
        root = tree(self)
        with self.assertRaisesRegex(SystemExit, "overlay.toml"):
            paths.overlay_dir(str(root))

    def test_env_used(self):
        root = tree(self)
        write(root / "overlay.toml", 'core_version = "v0.1.0"\ncore_writable = true\n')
        with mock.patch.dict(os.environ, {"CODE_JIG_OVERLAY": str(root)}):
            self.assertEqual(paths.overlay_dir(None), root)

    def test_codex_home_precedence(self):
        with mock.patch.dict(os.environ, {"CODEX_HOME": "/env/home"}):
            self.assertEqual(str(paths.codex_home("/arg/home")), str(paths.Path("/arg/home")))
            self.assertEqual(str(paths.codex_home(None)), str(paths.Path("/env/home")))


if __name__ == "__main__":
    unittest.main()


class Resolve(unittest.TestCase):
    def test_relative_overlay_becomes_absolute(self):
        import os

        from codejig import paths
        root = tree(self)
        write(root / "ov" / "overlay.toml", 'core_version = "v1"\ncore_writable = true\n')
        old = os.getcwd()
        os.chdir(root)
        self.addCleanup(os.chdir, old)
        self.assertTrue(paths.overlay_dir("ov").is_absolute())


class Examples(unittest.TestCase):
    def test_examples_pin_the_current_version_and_load(self):
        root = Path(__file__).resolve().parent.parent
        for name in ("personal", "enterprise"):
            with self.subTest(example=name):
                ov = load(root / "overlay-examples" / name)
                self.assertEqual(ov.core_version, f"v{VERSION}")
                self.assertTrue(tiers.load(root, ov.root))
                text = (root / "overlay-examples" / name / "AGENTS.overlay.md").read_text(encoding="utf-8")
                self.assertNotIn("bootstrap", text.lower())


class InitOverlay(unittest.TestCase):
    def run_cli(self, path):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = overlay.cli(types.SimpleNamespace(path=path))
        return code, out.getvalue(), err.getvalue()

    def test_copies_the_personal_example_and_prints_next_steps(self):
        target = tree(self) / "new overlay"
        code, out, _ = self.run_cli(str(target))
        self.assertEqual(code, 0)
        src = paths.CORE / "overlay-examples" / "personal"
        want = sorted(p.relative_to(src).as_posix() for p in src.rglob("*") if p.is_file())
        self.assertEqual(sorted(p.relative_to(target).as_posix() for p in target.rglob("*") if p.is_file()), want)
        self.assertEqual(load(target).core_version, f"v{VERSION}")
        self.assertIn("CODE_JIG_OVERLAY", out)
        self.assertIn("code-jig install", out)
        self.assertIn("code-jig doctor", out)

    def test_an_empty_folder_is_filled(self):
        target = tree(self) / "empty"
        target.mkdir()
        self.assertEqual(self.run_cli(str(target))[0], 0)
        self.assertTrue((target / "overlay.toml").is_file())

    def test_refuses_a_non_empty_folder_and_leaves_it(self):
        target = tree(self) / "mine"
        write(target / "notes.md", "keep\n")
        code, _, err = self.run_cli(str(target))
        self.assertEqual(code, 1)
        self.assertIn("not empty", err)
        self.assertEqual([p.name for p in target.iterdir()], ["notes.md"])

    def test_refuses_a_file(self):
        target = write(tree(self) / "file", "x")
        self.assertEqual(self.run_cli(str(target))[0], 1)
        self.assertEqual(target.read_text(encoding="utf-8"), "x")

    def test_default_target_is_found_without_flags(self):
        target = tree(self) / "code-jig-overlay"
        with mock.patch.object(paths, "default_overlay", return_value=target):
            code, out, _ = self.run_cli(None)
            self.assertEqual(code, 0)
            self.assertIn("finds this folder by default", out)
            with mock.patch.dict(os.environ, {}, clear=True):
                self.assertEqual(paths.overlay_dir(None), target)

    def test_parser_takes_an_optional_path(self):
        from codejig.__main__ import COMMAND_MODULES, build_parser
        self.assertEqual(build_parser().parse_args(["init-overlay"]).path, None)
        self.assertEqual(build_parser().parse_args(["init-overlay", "x"]).path, "x")
        self.assertEqual(COMMAND_MODULES["init-overlay"], "overlay")
