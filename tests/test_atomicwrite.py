import os
import stat
import unittest
from unittest import mock

from codejig import atomicwrite
from codejig.atomicwrite import atomic_write, resolve_inside
from tests.util import tree, write


class Inside(unittest.TestCase):
    def setUp(self):
        self.root = tree(self)
        self.outside = tree(self)

    def swap_for_link(self, folder):
        folder.rename(folder.with_name(folder.name + "-moved"))
        try:
            folder.symlink_to(self.outside, target_is_directory=True)
        except OSError:
            self.skipTest("no symlinks here")

    def test_a_folder_swapped_for_a_link_after_the_check_is_never_written_through(self):
        target = resolve_inside(str(write(self.root / "sub" / "x.txt", "agent!\n")), self.root)
        target.read_bytes()
        victim = write(self.outside / "x.txt", "mine\n")
        self.swap_for_link(self.root / "sub")
        with self.assertRaises(OSError):
            atomic_write(target, b"agent\n")
        self.assertEqual(victim.read_text(encoding="utf-8"), "mine\n")
        self.assertEqual(sorted(p.name for p in self.outside.iterdir()), ["x.txt"])

    def test_a_folder_swapped_before_the_read_is_not_read_through(self):
        target = resolve_inside(str(write(self.root / "sub" / "x.txt", "agent\n")), self.root)
        write(self.outside / "x.txt", "secret\n")
        self.swap_for_link(self.root / "sub")
        with self.assertRaises(OSError):
            target.read_bytes()

    def test_a_concurrent_edit_in_place_is_kept(self):
        path = write(self.root / "x.txt", "first\n")
        target = resolve_inside(str(path), self.root)
        original = target.read_bytes()
        with open(path, "r+b") as f:
            f.write(b"other\n")
        with self.assertRaises(atomicwrite.Changed):
            atomic_write(target, original.upper())
        self.assertEqual(path.read_bytes(), b"other\n")

    def test_a_file_replaced_after_the_check_is_kept(self):
        path = write(self.root / "x.txt", "first\n")
        target = resolve_inside(str(path), self.root)
        target.read_bytes()
        os.replace(write(self.root / "new.txt", "editor\n"), path)
        with self.assertRaises(atomicwrite.Changed):
            atomic_write(target, b"cleaned\n")
        self.assertEqual(path.read_bytes(), b"editor\n")

    def test_an_unchanged_file_is_rewritten_with_its_mode_and_no_temp(self):
        path = write(self.root / "sub" / "a.sh", "old\n")
        os.chmod(path, 0o750)
        target = resolve_inside(str(path), self.root)
        self.assertEqual(target.read_bytes(), b"old\n")
        atomic_write(target, b"new\n")
        self.assertEqual(path.read_bytes(), b"new\n")
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o750)
        self.assertEqual([p.name for p in path.parent.iterdir()], ["a.sh"])

    def test_without_dir_fds_the_path_is_checked_again_before_the_replace(self):
        with mock.patch.object(atomicwrite, "DIR_FDS", False):
            self.test_a_folder_swapped_for_a_link_after_the_check_is_never_written_through()
            self.test_a_concurrent_edit_in_place_is_kept()

    def test_derived_paths_are_plain_paths(self):
        target = resolve_inside(str(write(self.root / "sub" / "x.txt", "x\n")), self.root)
        write(self.root / "sub" / "y.txt", "y\n")
        self.assertEqual(target.with_name("y.txt").read_bytes(), b"y\n")


if __name__ == "__main__":
    unittest.main()
