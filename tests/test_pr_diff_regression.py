import os
import re
import shlex
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = (ROOT / ".github/workflows/validate-pr.yml").read_text()
COMMANDS = re.findall(r'git diff [^|\n]+?(?= \|)', WORKFLOW)


class PrDiffRegression(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name)
        self.git("init", "-q", "-b", "main")
        self.write("pool/main/cybercam/meta.json", "original camera")
        self.write("pool/main/stickzero/meta.json", "original stick")
        self.write("pool/main/stickzero/old.deb.release.json", "old release")
        self.write(".github/workflows/example.yml", "original workflow")
        self.start = self.commit("shared starting point")

    def git(self, *args):
        return subprocess.run(
            ["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
             "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null", *args],
            cwd=self.repo, check=True, text=True, capture_output=True,
        ).stdout.strip()

    def write(self, path, content):
        target = self.repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)

    def commit(self, message):
        self.git("add", "-A")
        self.git("commit", "-qm", message)
        return self.git("rev-parse", "HEAD")

    def fork(self):
        self.base = self.git("rev-parse", "HEAD")
        self.git("checkout", "-qb", "submission", self.start)

    def classify(self):
        head = self.git("rev-parse", "HEAD")
        merge_base = self.git("merge-base", self.base, head)
        result = {}
        for command in COMMANDS:
            actual = command.replace('"$DIFF_BASE_SHA"', merge_base).replace('"$HEAD_SHA"', head)
            args = shlex.split(actual)
            key = next((a.split("=")[1] for a in args if a.startswith("--diff-filter=")), "all")
            result[key] = set(self.git(*args[1:]).splitlines())
        return result

    def camera_change(self):
        self.write("pool/main/cybercam/meta.json", "camera permission fixed")
        self.commit("developer changes only CyberCam")

    def test_stale_fork_does_not_touch_another_package(self):
        self.write("pool/main/stickzero/meta.json", "USB controller clarification")
        self.commit("main updates StickZero")
        self.fork()
        self.camera_change()
        old = set(self.git("diff", "--name-only", self.base, "HEAD").splitlines())
        self.assertIn("pool/main/stickzero/meta.json", old)
        self.assertEqual(self.classify()["all"], {"pool/main/cybercam/meta.json"})

    def test_new_main_release_is_not_reported_as_deletion(self):
        self.write("pool/main/stickzero/new.deb.release.json", "new release")
        self.commit("main adds release")
        self.fork()
        self.camera_change()
        self.assertEqual(self.classify()["D"], set())

    def test_main_retirement_is_not_reported_as_submission(self):
        (self.repo / "pool/main/stickzero/old.deb.release.json").unlink()
        self.commit("main retires old release")
        self.fork()
        self.camera_change()
        self.assertEqual(self.classify()["A"], set())

    def test_main_manifest_update_is_not_reported_as_edit(self):
        self.write("pool/main/stickzero/old.deb.release.json", "main manifest change")
        self.commit("main updates manifest")
        self.fork()
        self.camera_change()
        self.assertEqual(self.classify()["M"], {"pool/main/cybercam/meta.json"})

    def test_main_workflow_update_does_not_trigger_outside_pool(self):
        self.write(".github/workflows/example.yml", "main updates workflow")
        self.commit("main updates workflow")
        self.fork()
        self.camera_change()
        self.assertTrue(all(p.startswith("pool/main/") for p in self.classify()["all"]))

    def test_actual_other_package_modification_is_still_visible(self):
        self.fork()
        self.write("pool/main/stickzero/meta.json", "actual forbidden edit")
        self.commit("submission really changes StickZero")
        self.assertIn("pool/main/stickzero/meta.json", self.classify()["all"])

    def test_actual_additions_and_deletions_remain_visible(self):
        self.fork()
        (self.repo / "pool/main/stickzero/old.deb.release.json").unlink()
        self.write("pool/main/cybercam/new.deb.release.json", "new camera release")
        self.write("pool/main/cybercam/raw.deb", "raw package")
        self.commit("submission adds and deletes real files")
        result = self.classify()
        self.assertEqual(result["D"], {"pool/main/stickzero/old.deb.release.json"})
        self.assertEqual(result["A"], {
            "pool/main/cybercam/new.deb.release.json", "pool/main/cybercam/raw.deb",
        })

    def test_cross_package_move_keeps_both_paths(self):
        self.fork()
        self.git("mv", "pool/main/stickzero/meta.json", "pool/main/cybercam/moved.json")
        self.commit("move from another package")
        result = self.classify()
        self.assertIn("pool/main/stickzero/meta.json", result["D"])
        self.assertIn("pool/main/cybercam/moved.json", result["A"])

    def test_all_six_diff_calls_use_merge_base_but_policy_uses_latest_base(self):
        self.assertEqual(len(COMMANDS), 6)
        self.assertTrue(all("--no-renames" in c and '"$DIFF_BASE_SHA".."$HEAD_SHA"' in c for c in COMMANDS))
        self.assertNotIn('"$BASE_SHA".."$HEAD_SHA"', WORKFLOW)
        self.assertIn('git ls-tree -r "$BASE_SHA"', WORKFLOW)
        self.assertIn('git show "$BASE_SHA:$manifest"', WORKFLOW)
        self.assertIn('git show "$BASE_SHA:.github/scripts/$script"', WORKFLOW)

    def test_missing_revision_produces_failure_result(self):
        match = re.search(r"          if ! DIFF_BASE_SHA=.*?\n          fi", WORKFLOW, re.S)
        self.assertIsNotNone(match)
        out = self.repo / "out"
        out.mkdir()
        script = "\n".join(line[10:] for line in match.group().splitlines())
        script = script.replace("/tmp/out", str(out))
        output = self.repo / "github-output"
        env = {**os.environ, "BASE_SHA": "missing-base", "HEAD_SHA": self.start, "GITHUB_OUTPUT": str(output)}
        subprocess.run(["bash", "-c", script], cwd=self.repo, env=env, check=True, capture_output=True)
        self.assertEqual(output.read_text().strip(), "status=failure")
        self.assertIn("Unable to determine", (out / "result.md").read_text())


if __name__ == "__main__":
    unittest.main(verbosity=2)
