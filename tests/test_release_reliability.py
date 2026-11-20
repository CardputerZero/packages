"""Regression coverage for bounded OSS operations, visibility and source links."""
import importlib.util
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "oss"))
sys.path.insert(0, str(ROOT / ".github/scripts"))

import oss_command
import wait_pages_artifact
from clone_source_repo import clone_args, needs_source_checkout

spec = importlib.util.spec_from_file_location("upload_files", ROOT / "oss/upload-files.py")
upload = importlib.util.module_from_spec(spec)
spec.loader.exec_module(upload)


class OSSCommandTests(unittest.TestCase):
    def test_timeout_retries_once(self):
        with patch.object(oss_command.subprocess, "run", side_effect=[
            subprocess.TimeoutExpired("ossutil", 30), subprocess.CompletedProcess([], 0, "ok")
        ]) as run, patch.object(oss_command.time, "sleep") as sleep:
            self.assertEqual(oss_command.command("stat", "oss://bucket/pool/file"), "ok")
        self.assertEqual(run.call_count, 2)
        self.assertEqual(run.call_args.kwargs["timeout"], 30)
        sleep.assert_called_once_with(2)

    def test_permission_failure_is_not_retried(self):
        with patch.object(oss_command.subprocess, "run", return_value=subprocess.CompletedProcess(
            [], 1, "StatusCode=403, ErrorCode=AccessDenied"
        )) as run, self.assertRaisesRegex(RuntimeError, "403"):
            oss_command.command("stat", "oss://bucket/pool/file")
        self.assertEqual(run.call_count, 1)

    def test_timeout_cannot_wait_indefinitely(self):
        with patch.object(oss_command.subprocess, "run", side_effect=subprocess.TimeoutExpired([], 120)) as run, \
             patch.object(oss_command.time, "sleep"), self.assertRaisesRegex(RuntimeError, "timed out"):
            oss_command.command("cp", "-f", "file", "oss://bucket/pool/file")
        self.assertEqual(run.call_count, 2)
        self.assertEqual(run.call_args.kwargs["timeout"], 120)


class InventoryTests(unittest.TestCase):
    def setUp(self):
        self.prefix = "oss://bucket/packages/pool/"
        self.key = self.prefix + "a/with space.png"
        self.row = f"2026-01-01 00:00:00 +0000 UTC        3     Standard   AAAAAAAA  {self.key}"
        self.identity = ["2026-01-01 00:00:00 +0000 UTC", 3, "aaaaaaaa"]

    def test_complete_listing_and_spaces(self):
        listing = f"LastModifiedTime   Size(B) StorageClass ETAG ObjectName\n{self.row}\nObject Number is: 1\n0.01(s) elapsed\n"
        self.assertEqual(upload.parse_inventory(listing, self.prefix), {self.key: self.identity})

    def test_ambiguous_or_incomplete_inventory_is_not_used(self):
        for listing in (self.row, f"{self.row}\nObject Number is: 2", f"{self.row}\n{self.row}\nObject Number is: 2",
                        f"{self.row.replace(self.prefix, 'oss://other/')}\nObject Number is: 1"):
            with self.subTest(listing=listing), self.assertRaises(ValueError):
                upload.parse_inventory(listing, self.prefix)

    def test_only_content_and_identity_matches_skip_remote_checks(self):
        with tempfile.TemporaryDirectory() as d:
            root = pathlib.Path(d)
            path = root / "pool/a/with space.png"
            path.parent.mkdir(parents=True)
            path.write_bytes(b"abc")
            relative = path.relative_to(root).as_posix()
            record = {"sha256": upload.sha256(path), "object": self.identity}
            state = {relative: record}
            with patch.object(upload, "command") as oss:
                self.assertEqual(upload.sync_file(root, "oss://bucket/packages", path, {self.key: self.identity}, state), (0, record, True))
                oss.assert_not_called()
            # A same-size change, on either side, must fall back to CRC.
            path.write_bytes(b"xyz")
            with patch.object(upload, "command", side_effect=["", ""]) as oss:
                self.assertEqual(upload.sync_file(root, "oss://bucket/packages", path, {self.key: self.identity}, state)[0], 1)
                self.assertEqual(oss.call_args_list[0].args[0], "stat")

    def test_cache_is_scoped_to_endpoint_and_destination(self):
        with tempfile.TemporaryDirectory() as d:
            path = pathlib.Path(d) / "state.json"
            upload.save_state(path, ["endpoint", "oss://bucket/a"], {"file": "record"})
            self.assertEqual(upload.load_state(path, ["endpoint", "oss://bucket/b"]), {})
            self.assertEqual(upload.load_state(path, ["endpoint", "oss://bucket/a"]), {"file": "record"})


class ArtifactTests(unittest.TestCase):
    def test_waits_for_exact_nonexpired_artifact(self):
        observed = [
            {"artifacts": [{"id": 1, "name": "withdrawal-plan"}]},
            {"artifacts": [{"id": 2, "name": "github-pages", "size_in_bytes": 10, "expired": True}]},
            {"artifacts": [{"id": 2, "name": "github-pages", "size_in_bytes": 10, "expired": False}]},
        ]
        with patch.object(wait_pages_artifact.subprocess, "run", side_effect=[
            subprocess.CompletedProcess([], 0, json.dumps(x)) for x in observed
        ]) as run, patch.object(wait_pages_artifact.time, "sleep"):
            wait_pages_artifact.wait_for_artifact("owner/repo", "12", "2")
        self.assertEqual(run.call_count, 3)

    def test_missing_artifact_fails_before_deployment(self):
        with patch.object(wait_pages_artifact.subprocess, "run", return_value=subprocess.CompletedProcess(
            [], 0, '{"artifacts":[]}'
        )), patch.object(wait_pages_artifact.time, "sleep"), self.assertRaisesRegex(RuntimeError, "never became visible"):
            wait_pages_artifact.wait_for_artifact("owner/repo", "12", "2", attempts=2)


class SourceRepoTests(unittest.TestCase):
    def test_explicit_portal_metadata_does_not_depend_on_source_clone(self):
        self.assertFalse(needs_source_checkout('{"title":"Wechat"}'))
        self.assertTrue(needs_source_checkout("null"))
        self.assertTrue(needs_source_checkout(None))

    def test_github_branch_page_preserves_ref(self):
        self.assertEqual(clone_args("https://github.com/Apeadv/Wechat-for-cardputer-zero/tree/main", "/tmp/src"),
                         ["git", "clone", "--depth=1", "--branch", "main", "--",
                          "https://github.com/Apeadv/Wechat-for-cardputer-zero.git", "/tmp/src"])
        self.assertIn("feature/example", clone_args("https://github.com/a/b/tree/feature%2Fexample", "src"))

    def test_regular_repos_unchanged_or_canonicalized(self):
        self.assertEqual(clone_args("https://github.com/a/b.git/", "src")[-2], "https://github.com/a/b.git")
        self.assertEqual(clone_args("https://gitlab.com/a/b.git", "src")[-2], "https://gitlab.com/a/b.git")

    def test_invalid_urls_do_not_become_git_options(self):
        for url in ("--help", "file:///tmp/local", "https://user:token@github.com/a/b", "https://github.com/a/b/issues/1"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                clone_args(url, "src")


if __name__ == "__main__":
    unittest.main()
