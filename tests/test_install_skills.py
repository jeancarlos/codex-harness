import shutil
import unittest
from unittest import mock

from codejig import install
from codejig.install import SKILL_SIDECAR, install_skills, skill_sources, tree_digest
from tests.util import tree, write

SKILL = "---\nname: {n}\ndescription: test skill {n}\n---\n\nbody\n"


def skill(root, name, body=None):
    write(root / name / "SKILL.md", body or SKILL.format(n=name))
    return root / name


class Skills(unittest.TestCase):
    def setUp(self):
        self.root = tree(self)
        self.core = self.root / "core-repo"
        self.ov = self.root / "overlay"
        self.dest = self.root / "agents" / "skills"
        skill(self.core / "skills", "grilling")

    def test_sources_and_clash(self):
        skill(self.ov / "skills", "mine")
        self.assertEqual(sorted(skill_sources(self.core, self.ov)), ["grilling", "mine"])
        skill(self.ov / "skills", "grilling")
        with self.assertRaisesRegex(ValueError, "grilling"):
            skill_sources(self.core, self.ov)

    def test_install_copies_with_sidecar(self):
        install_skills(skill_sources(self.core, self.ov), self.dest)
        got = self.dest / "grilling"
        self.assertEqual((got / "SKILL.md").read_text(encoding="utf-8"), SKILL.format(n="grilling"))
        self.assertEqual((got / SKILL_SIDECAR).read_text(encoding="utf-8").strip(), tree_digest(got))

    def test_user_skill_with_same_name_is_backed_up(self):
        skill(self.dest, "grilling", "---\nname: grilling\ndescription: mine\n---\nmine\n")
        lines = install_skills(skill_sources(self.core, self.ov), self.dest)
        self.assertEqual(list(self.dest.glob("grilling.bak-*")), [])
        [backup] = (self.dest.parent / "skills-backup").glob("grilling.bak-*")
        self.assertIn("mine", (backup / "SKILL.md").read_text(encoding="utf-8"))
        self.assertTrue(any(str(backup) in line for line in lines))

    def test_reinstall_replaces_own_copy_without_backup(self):
        install_skills(skill_sources(self.core, self.ov), self.dest)
        write(self.core / "skills" / "grilling" / "SKILL.md", SKILL.format(n="grilling") + "v2\n")
        install_skills(skill_sources(self.core, self.ov), self.dest)
        self.assertEqual(list(self.dest.glob("grilling.bak-*")), [])
        self.assertIn("v2", (self.dest / "grilling" / "SKILL.md").read_text(encoding="utf-8"))

    def test_removed_skill_pruned_edited_copy_kept(self):
        skill(self.ov / "skills", "a")
        skill(self.ov / "skills", "b")
        install_skills(skill_sources(self.core, self.ov), self.dest)
        write(self.dest / "b" / "SKILL.md", "---\nname: b\ndescription: edited\n---\n")
        import shutil
        shutil.rmtree(self.ov / "skills" / "a")
        shutil.rmtree(self.ov / "skills" / "b")
        lines = install_skills(skill_sources(self.core, self.ov), self.dest)
        self.assertFalse((self.dest / "a").exists())
        self.assertTrue((self.dest / "b").exists())
        self.assertTrue(any(line.startswith("warning:") and "b" in line for line in lines))

    def test_unrelated_user_skills_untouched(self):
        mine = skill(self.dest, "other")
        install_skills(skill_sources(self.core, self.ov), self.dest)
        self.assertTrue((mine / "SKILL.md").exists())


    def test_evals_added_to_an_installed_copy_are_kept(self):
        install_skills(skill_sources(self.core, self.ov), self.dest)
        write(self.dest / "grilling" / "evals" / "case" / "a.txt", "mine\n")
        lines = install_skills(skill_sources(self.core, self.ov), self.dest)
        [backup] = (self.dest.parent / "skills-backup").glob("grilling.bak-*")
        self.assertEqual((backup / "evals" / "case" / "a.txt").read_text(encoding="utf-8"), "mine\n")
        self.assertTrue(any(str(backup) in line for line in lines))
        write(self.dest / "grilling" / "evals.toml", "[[case]]\n")
        shutil.rmtree(self.core / "skills" / "grilling")
        lines = install_skills(skill_sources(self.core, self.ov), self.dest)
        self.assertTrue((self.dest / "grilling" / "evals.toml").is_file())
        self.assertTrue(any(line.startswith("warning:") for line in lines))

    def test_a_failed_copy_keeps_the_installed_skill(self):
        install_skills(skill_sources(self.core, self.ov), self.dest)
        write(self.core / "skills" / "grilling" / "SKILL.md", SKILL.format(n="grilling") + "v2\n")
        with mock.patch.object(install.shutil, "copytree", side_effect=OSError("disk full")), self.assertRaises(OSError):
            install_skills(skill_sources(self.core, self.ov), self.dest)
        self.assertEqual((self.dest / "grilling" / "SKILL.md").read_text(encoding="utf-8"), SKILL.format(n="grilling"))
        self.assertEqual(sorted(p.name for p in self.dest.parent.iterdir()), ["skills"])
        install_skills(skill_sources(self.core, self.ov), self.dest)
        self.assertIn("v2", (self.dest / "grilling" / "SKILL.md").read_text(encoding="utf-8"))
        self.assertEqual(sorted(p.name for p in self.dest.parent.iterdir()), ["skills"])

    def test_a_symlink_in_a_skill_source_is_refused(self):
        secret = write(self.root / "secret" / "id_rsa", "KEY\n")
        mine = skill(self.ov / "skills", "mine")
        try:
            (mine / "notes.md").symlink_to(secret)
        except OSError:
            self.skipTest("no symlinks here")
        with self.assertRaisesRegex(ValueError, "symlink"):
            skill_sources(self.core, self.ov)
        (mine / "notes.md").unlink()
        (mine / "refs").symlink_to(secret.parent, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "symlink"):
            skill_sources(self.core, self.ov)
        self.assertFalse(self.dest.exists())


class RealHomeIsolation(unittest.TestCase):
    def test_tests_never_target_the_real_skills_folder(self):
        from pathlib import Path

        from codejig import paths
        self.assertNotEqual(paths.agents_skills(), Path.home() / ".agents" / "skills")

    def test_tests_never_target_the_real_claude_settings(self):
        from pathlib import Path

        from codejig import paths
        self.assertNotEqual(paths.claude_code_settings(), Path.home() / ".claude" / "settings.json")
