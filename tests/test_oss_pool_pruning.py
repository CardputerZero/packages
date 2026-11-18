"""Validate complete, scoped deletion plans before touching OSS objects."""
import importlib.util
from pathlib import Path
import tempfile
import unittest


spec = importlib.util.spec_from_file_location(
    "prune_pool", Path(__file__).resolve().parents[1] / "oss/prune-pool.py")
prune_pool = importlib.util.module_from_spec(spec)
spec.loader.exec_module(prune_pool)


class PoolPruningTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.destination = "oss://bucket/packages/pool/"
        (self.root / "current with space.deb").write_bytes(b"current")

    def plan(self, listing, destination=None):
        return prune_pool.stale_objects(self.root, destination or self.destination, listing)

    def test_preserves_current_file_and_plans_only_withdrawn_file(self):
        current = self.destination + "current with space.deb"
        old = self.destination + "old with space.deb"
        self.assertEqual(self.plan(f"{current}\n{old}\nObject Number is: 2\n"), [old])

    def test_empty_complete_listing_is_safe(self):
        self.assertEqual(self.plan("Object Number is: 0\n"), [])

    def test_truncated_duplicate_or_ambiguous_listing_fails(self):
        obj = self.destination + "old.deb"
        for listing in (f"{obj}\n", f"{obj}\nObject Number is: 2\n",
                        f"{obj}\n{obj}\nObject Number is: 2\n",
                        f"{obj}\nObject Number is: 1\nObject Number is: 1\n",
                        f"{obj}\ncontinued-name\nObject Number is: 1\n"):
            with self.subTest(listing=listing), self.assertRaises(ValueError):
                self.plan(listing)

    def test_outside_object_or_destination_fails(self):
        for obj in ("oss://bucket/os-list.json", "oss://bucket/packages/pool-backup/old.deb"):
            with self.subTest(obj=obj), self.assertRaises(ValueError):
                self.plan(f"{obj}\nObject Number is: 1\n")
        for destination in ("oss://bucket/", "oss://bucket/packages/", "/tmp/pool/"):
            with self.subTest(destination=destination), self.assertRaises(ValueError):
                self.plan("Object Number is: 0\n", destination)


if __name__ == "__main__":
    unittest.main()
