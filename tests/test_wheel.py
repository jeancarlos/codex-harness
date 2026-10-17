import importlib.util
import io
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from codejig import VERSION, paths
from codejig.doctor import check_core, package_digest
from codejig.overlay import Overlay

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / ".github" / "scripts"))
import build_wheel

TAG = f"v{VERSION}"
CORE_DATA = {"core", "skills", "bench", "overlay-examples", "VERSION"}
CAN_BUILD = all(importlib.util.find_spec(m) for m in ("build", "hatchling"))


def files(root: Path) -> list[str]:
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file())


@unittest.skipUnless((ROOT / ".git").exists(), "needs a git checkout")
class Stage(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        build_wheel.stage(TAG, cls.tmp)
        cls.code = cls.tmp / "codejig"
        cls.core = cls.code / "_core"

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, True)

    def test_package_is_the_code_and_the_data(self):
        self.assertEqual({p.name for p in self.core.iterdir()}, CORE_DATA)
        self.assertEqual([p.name for p in (self.core / "bench").iterdir()], ["requirements.toml"])
        tracked = subprocess.run(["git", "-C", str(ROOT), "ls-files", "codejig"], capture_output=True, text=True, check=True).stdout
        self.assertEqual({p.name for p in self.code.glob("*.py")}, {Path(n).name for n in tracked.split()})
        self.assertTrue((self.core / "overlay-examples" / "personal" / "overlay.toml").is_file())

    def test_nothing_of_the_old_installer_or_the_repo_tooling_is_staged(self):
        top = {n.split("/")[0] for n in files(self.tmp)}
        self.assertTrue((self.tmp / "pyproject.toml").is_file())
        for gone in ("bootstrap.py", "install.sh", "install.ps1", "worker", ".github", "ci", "fuzz", "tests", "docs", "core", "skills"):
            self.assertNotIn(gone, top)

    def test_version_names_the_tag_and_the_package_digest(self):
        tag, digest = (self.core / "VERSION").read_text(encoding="utf-8").split()
        self.assertEqual((tag, digest), (TAG, package_digest(self.code)))

    def test_no_eval_fixtures_ship(self):
        names = files(self.core)
        self.assertFalse([n for n in names if "/evals/" in f"/{n}" or n.endswith(("evals.toml", ".skill-check"))])
        self.assertFalse([n for n in files(self.tmp) if "__pycache__" in n])

    def test_doctor_catches_an_edited_module_or_data_file_in_a_copy(self):
        for rel in ("codejig/gitgate.py", "codejig/_core/core/AGENTS.core.md"):
            with self.subTest(rel=rel), tempfile.TemporaryDirectory() as d:
                code = Path(d) / "codejig"
                shutil.copytree(self.code, code)
                ov = Overlay(code / "_core", TAG, False)
                self.assertEqual(check_core(code / "_core", ov)[1][:2], ("ok", "core clean"))
                target = Path(d) / rel
                target.write_bytes(target.read_bytes() + b"\n")
                self.assertEqual(check_core(code / "_core", ov)[1][:2], ("FAIL", "core clean"))

    def test_a_wrong_tag_is_refused(self):
        with self.assertRaises(SystemExit):
            build_wheel.build("v9.9.9", self.tmp / "out")


@unittest.skipUnless((ROOT / ".git").exists() and CAN_BUILD, "needs a git checkout with build and hatchling")
class Wheel(unittest.TestCase):
    def test_the_built_wheel_holds_exactly_the_staged_package(self):
        with tempfile.TemporaryDirectory() as d:
            out, staged = Path(d) / "dist", Path(d) / "staged"
            build_wheel.build(TAG, out)
            build_wheel.stage(TAG, staged)
            [whl] = out.glob("*.whl")
            with zipfile.ZipFile(whl) as zf:
                names = zf.namelist()
                packaged = sorted(n for n in names if n.startswith("codejig/"))
                self.assertEqual(packaged, ["codejig/" + n for n in files(staged / "codejig")])
                self.assertFalse([n for n in names if not n.startswith(("codejig/", "code_jig-"))])
                entry = next(n for n in names if n.endswith(".dist-info/entry_points.txt"))
                self.assertIn("cjig = codejig.__main__:main", zf.read(entry).decode())
                zf.extractall(Path(d) / "site")
            self.assertEqual(check_core(Path(d) / "site" / "codejig" / "_core", Overlay(Path(d), TAG, False))[1][0], "ok")


class Tags(unittest.TestCase):
    def test_release_candidates_are_tags_and_other_suffixes_are_not(self):
        for tag in ("v1.2.3", "v1.2.3-rc1", "v10.0.12-rc22"):
            self.assertTrue(build_wheel.TAG.fullmatch(tag), tag)
        for tag in ("v1.2", "v1.2.3-beta", "v1.2.3-rc", "1.2.3", "v1.2.3-rc1x", "v1.2.3rc1"):
            self.assertFalse(build_wheel.TAG.fullmatch(tag), tag)

    def test_the_tag_must_be_the_package_version(self):
        with mock.patch.object(build_wheel, "VERSION", "1.2.3-rc1"):
            self.assertTrue(build_wheel.valid("v1.2.3-rc1"))
            for tag in ("v1.2.3", "v1.2.3-rc2", "nightly"):
                self.assertFalse(build_wheel.valid(tag), tag)
        with mock.patch.object(build_wheel, "VERSION", "1.2.3"):
            self.assertTrue(build_wheel.valid("v1.2.3"))
            self.assertFalse(build_wheel.valid("v1.2.3-rc1"))

    def test_no_arguments_builds_the_current_version_into_dist(self):
        with mock.patch.object(build_wheel, "build") as build:
            build_wheel.main([])
            build.assert_called_once_with(TAG, ROOT / "dist")
            build_wheel.main(["v1.2.3", "out"])
            build.assert_called_with("v1.2.3", Path("out"))


class Editable(unittest.TestCase):
    def test_core_is_the_packaged_data_when_it_exists(self):
        with tempfile.TemporaryDirectory() as d:
            code = Path(d) / "codejig"
            (code / "_core").mkdir(parents=True)
            self.assertEqual(paths.locate_core(code), code / "_core")

    def test_a_checkout_or_editable_install_uses_the_repo_root(self):
        self.assertEqual(paths.locate_core(paths.CODE), ROOT)
        self.assertEqual(paths.CORE, ROOT)


class ScorecardSeesTheRepo(unittest.TestCase):
    @unittest.skipUnless((ROOT / ".git").exists(), "needs a git checkout")
    def test_the_github_tarball_keeps_what_scorecard_reads(self):
        out = subprocess.run(["git", "-C", str(ROOT), "archive", "--format=tar", "HEAD"], check=True, capture_output=True).stdout
        with tarfile.open(fileobj=io.BytesIO(out)) as tar:
            names = tar.getnames()
        for needed in (".github/dependabot.yml", ".github/workflows/ci.yml", "SECURITY.md", "ci/analyze.txt"):
            self.assertIn(needed, names)


if __name__ == "__main__":
    unittest.main()
