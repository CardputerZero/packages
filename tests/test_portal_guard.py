"""Exercise the dispatch guard with the same Git and shell commands used by Actions."""
import os
from pathlib import Path
import subprocess
import tempfile
import textwrap
import unittest


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = (ROOT / ".github/workflows/process-web-submission.yml").read_text()
GUARD = textwrap.dedent(WORKFLOW.split("id: guard\n", 1)[1].split("run: |\n", 1)[1].split("\n      - name:", 1)[0])


class PortalGuardTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.origin, self.checkout = root / "origin", root / "runner"
        subprocess.run(["git", "init", "-qb", "main", self.origin], check=True)
        self.commit("pool/main/sample/meta.json")
        subprocess.run(["git", "clone", "-q", self.origin.as_uri(), self.checkout], check=True)
        # The shared module's real PR matching is tested in the portal. Stub only
        # that read here so the shell exercises real fetch/diff/checkout behavior.
        self.env = {**os.environ, "PKG": "sample", "GITHUB_OUTPUT": str(root / "output"),
                    "PATH": str(root) + os.pathsep + os.environ["PATH"]}
        lookup = root / "node"
        lookup.write_text('#!/bin/sh\ncat >/dev/null\nprintf "%s\\n" "$PENDING"\n')
        lookup.chmod(0o755)

    def commit(self, name):
        path = self.origin / name
        path.parent.mkdir(exist_ok=True, parents=True)
        with path.open("a") as stream:
            stream.write("change\n")
        subprocess.run(["git", "add", name], cwd=self.origin, check=True)
        subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@example.org",
                        "-c", "commit.gpgSign=false", "commit", "-qm", "change"], cwd=self.origin, check=True)

    def guard(self, pending=""):
        return subprocess.run(["bash", "-c", GUARD], cwd=self.checkout,
                              env={**self.env, "PENDING": pending}, capture_output=True, text=True)

    def test_pending_review_and_stale_event(self):
        result = self.guard("953")
        self.assertEqual(result.returncode, 1)
        self.assertIn("#953", Path(self.env["GITHUB_OUTPUT"]).read_text())
        self.commit("pool/main/sample/meta.json")
        result = self.guard()
        self.assertEqual(result.returncode, 1)
        self.assertIn("changed after submission", Path(self.env["GITHUB_OUTPUT"]).read_text())

    def test_unrelated_change_and_recent_submission_can_continue(self):
        self.commit("pool/main/other/meta.json")
        result = self.guard()
        self.assertEqual(result.returncode, 0, result.stderr)
        def revision(repo):
            return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo)
        self.assertEqual(revision(self.checkout), revision(self.origin))
