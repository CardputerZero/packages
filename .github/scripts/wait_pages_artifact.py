"""Wait for the exact uploaded Pages artifact to become visible to GitHub."""
import json
import os
import subprocess
import time


def wait_for_artifact(repo, run_id, artifact_id, attempts=12, delay=5):
    if not str(artifact_id).isdigit():
        raise ValueError("Pages upload did not return an artifact ID")
    endpoint = f"repos/{repo}/actions/runs/{run_id}/artifacts?name=github-pages&per_page=100"
    for attempt in range(1, attempts + 1):
        try:
            result = subprocess.run(["gh", "api", endpoint], check=True,
                                    stdout=subprocess.PIPE, text=True, timeout=10)
            artifacts = json.loads(result.stdout)["artifacts"]
            if any(str(a.get("id")) == str(artifact_id) and a.get("name") == "github-pages"
                   and not a.get("expired") and a.get("size_in_bytes", 0) > 0 for a in artifacts):
                print(f"Pages artifact {artifact_id} is visible (attempt {attempt})", flush=True)
                # REST and the Actions artifact service can propagate at slightly
                # different times. Give the latter a brief settling window.
                time.sleep(3)
                return
        except (subprocess.SubprocessError, ValueError, KeyError) as exc:
            print(f"Artifact lookup attempt {attempt}: {exc}", flush=True)
        if attempt < attempts:
            print(f"Waiting for Pages artifact {artifact_id} ({attempt}/{attempts})", flush=True)
            time.sleep(delay)
    raise RuntimeError(f"Pages artifact {artifact_id} never became visible; deployment not attempted")


if __name__ == "__main__":
    wait_for_artifact(os.environ["GITHUB_REPOSITORY"], os.environ["GITHUB_RUN_ID"],
                      os.environ["PAGES_ARTIFACT_ID"])
