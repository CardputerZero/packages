"""Accept ordinary GitHub repo links as well as /tree/<branch-or-tag> links."""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
from urllib.parse import unquote, urlsplit


def clone_args(url, destination):
    url = url.strip()
    parsed = urlsplit(url)
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname
            or parsed.username or parsed.password or any(c in url for c in "\r\n\t")):
        raise ValueError("source_repo must be an HTTP(S) repository URL without credentials")
    branch = None
    if parsed.hostname.lower() == "github.com":
        paths = parsed.path.strip("/").split("/")
        if len(paths) < 2 or any(not re.fullmatch(r"[A-Za-z0-9_.-]+", p) or p in {".", ".."} for p in paths[:2]):
            raise ValueError("expected a GitHub repository URL")
        owner, repo = paths[:2]
        repo = repo.removesuffix(".git")
        if not repo:
            raise ValueError("expected a GitHub repository name")
        if len(paths) > 2:
            if paths[2] != "tree" or len(paths) < 4:
                raise ValueError("use a GitHub repository or /tree/<branch-or-tag> URL")
            branch = unquote("/".join(paths[3:]))
            if not branch or any(c in branch for c in "\r\n\t"):
                raise ValueError("invalid GitHub branch or tag")
        url = f"https://github.com/{owner}/{repo}.git"
    args = ["git", "clone", "--depth=1"]
    if branch is not None:
        args += ["--branch", branch]
    return args + ["--", url, str(destination)]


def needs_source_checkout(store_json):
    # Portal-provided metadata and asset URLs are consumed directly. A source
    # repository is required only for the legacy app-builder metadata path.
    try:
        return not isinstance(json.loads(store_json or "null"), dict)
    except ValueError:
        return True


def clone(url, destination):
    destination = Path(destination)
    if destination.exists():
        raise ValueError(f"source destination already exists: {destination}")
    # Validate before any retry; clone into a fresh private directory each time.
    clone_args(url, destination)
    for attempt in range(1, 3):
        with tempfile.TemporaryDirectory(prefix="source-checkout-", dir=destination.parent) as temporary:
            checkout = Path(temporary) / "repo"
            try:
                subprocess.run(clone_args(url, checkout), check=True, timeout=45,
                               env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
                checkout.rename(destination)
                return
            except subprocess.SubprocessError as exc:
                if attempt == 2:
                    raise
                print(f"Source checkout failed (attempt {attempt}): {exc}; retrying", file=sys.stderr)
        time.sleep(3)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url")
    parser.add_argument("destination", type=Path)
    parser.add_argument("--skip-if-web-metadata", action="store_true")
    args = parser.parse_args()
    try:
        if args.skip_if_web_metadata and not needs_source_checkout(os.environ.get("STORE_JSON")):
            print("Using portal-provided store metadata; source checkout is not needed")
        else:
            clone(args.url, args.destination)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f"Source repository clone failed: {exc}", file=sys.stderr)
        sys.exit(1)
