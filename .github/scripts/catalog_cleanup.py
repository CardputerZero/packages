#!/usr/bin/env python3
"""Reconcile withdrawn versions from merged main history, never from open PRs."""
import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request


MANIFEST = re.compile(r"^pool/main/([a-z0-9][a-z0-9.+-]+)/([^/]+\.deb)\.release\.json$")


def git(*args):
    return subprocess.check_output(["git", *args], text=True).strip()


def historical_manifest(path):
    """A deletion in main's first-parent history is an approved withdrawal."""
    commit = git("log", "--first-parent", "-1", "--diff-filter=D", "--format=%H", "HEAD", "--", path)
    if not commit:
        return None
    return json.loads(git("show", f"{commit}^:{path}"))


def plan():
    if git("rev-parse", "--is-shallow-repository") != "false":
        raise ValueError("full main history is required for withdrawal cleanup")
    active = {}
    for path in Path("pool/main").glob("*/*.deb.release.json"):
        match = MANIFEST.fullmatch(path.as_posix())
        if not match:
            raise ValueError(f"invalid manifest path: {path}")
        data = json.loads(path.read_text())
        if data["filename"] != match[2] or data["package"] != match[1]:
            raise ValueError(f"manifest identity mismatch: {path}")
        active[data["filename"]] = data
    deleted = git("log", "--first-parent", "--format=", "--name-only", "--diff-filter=D", "HEAD", "--", "pool/main")
    withdrawn = []
    for path in sorted(set(deleted.splitlines())):
        match = MANIFEST.fullmatch(path)
        if not match or match[2] in active:
            continue
        data = historical_manifest(path)
        if not data or data["filename"] != match[2] or data["package"] != match[1]:
            raise ValueError(f"invalid withdrawn manifest: {path}")
        withdrawn.append(data)
    return {"sha": git("rev-parse", "HEAD"), "active": list(active.values()), "withdrawn": withdrawn}


def prune_tree(root):
    """Do not serve icons, screenshots or meta.json after the last version goes."""
    for directory in root.iterdir():
        if directory.is_dir() and not list(directory.glob("*.deb.release.json")):
            shutil.rmtree(directory)


def gh_json(endpoint):
    return json.loads(subprocess.check_output(["gh", "api", endpoint], text=True))


def release_cleanup(data, dry_run=False):
    repo = os.environ["GITHUB_REPOSITORY"]
    active = {m["filename"] for m in data["active"]}
    active_packages = {m.get("package") for m in data["active"]}
    # Publishing may advance main during a long deployment. Protect any newly
    # (re)published filenames as well, without blocking unrelated withdrawals.
    sha = gh_json(f"repos/{repo}/commits/main")["sha"]
    if sha != data["sha"]:
        tree = gh_json(f"repos/{repo}/git/trees/{sha}?recursive=1")
        if tree.get("truncated"):
            raise ValueError("current main tree is truncated; refusing asset deletion")
        active.update(MANIFEST.fullmatch(t["path"])[2] for t in tree["tree"]
                      if t["type"] == "blob" and MANIFEST.fullmatch(t["path"]))
        active_packages.update(MANIFEST.fullmatch(t["path"])[1] for t in tree["tree"]
                               if t["type"] == "blob" and MANIFEST.fullmatch(t["path"]))
    withdrawn = {m["filename"] for m in data["withdrawn"]} - active
    retired_image_prefixes = {f"{m['uploaded_by']}__{m['package']}__".lower()
                              for m in data["withdrawn"]
                              if m.get("uploaded_by") and m.get("package") not in active_packages}
    # List releases rather than swallowing API failures as an absent tag.
    releases = json.loads(subprocess.check_output(
        ["gh", "api", "--paginate", "--slurp", f"repos/{repo}/releases?per_page=100"], text=True))
    for tag in ("apt-pool", "staging", "czdev-buffer"):
        release = next((r for page in releases for r in page if r["tag_name"] == tag), None)
        if release is None:
            continue
        pages = json.loads(subprocess.check_output(
            ["gh", "api", "--paginate", "--slurp", f"repos/{repo}/releases/{release['id']}/assets?per_page=100"], text=True))
        for asset in (a for page in pages for a in page):
            # Portal uploads have a login prefix; czdev uploads do not.
            filename = asset["name"].split("__", 1)[-1]
            retired_image = (tag == "staging" and asset["name"].lower().endswith(".png")
                             and any(asset["name"].lower().startswith(p) for p in retired_image_prefixes))
            if filename not in withdrawn and not retired_image:
                continue
            print(f"{'would delete' if dry_run else 'delete'} {tag}/{asset['name']}", flush=True)
            if not dry_run:
                subprocess.run(["gh", "api", "--method", "DELETE", f"repos/{repo}/releases/assets/{asset['id']}"], check=True)


def verify_registries(data, urls, attempts=20):
    withdrawn = {(m["package"], m["version"]) for m in data["withdrawn"]}
    active = {(m["package"], m["version"]) for m in data["active"]}
    withdrawn -= active
    for attempt in range(attempts):
        errors = []
        for url in urls:
            try:
                separator = "&" if "?" in url else "?"
                req = urllib.request.Request(url + separator + "withdrawal_check=" + str(time.time_ns()),
                                             headers={"Cache-Control": "no-cache", "User-Agent": "cz-withdrawal-check"})
                with urllib.request.urlopen(req, timeout=30) as response:
                    registry = json.load(response)
                if not isinstance(registry.get("apps"), list):
                    raise ValueError("invalid registry schema")
                versions = {(a.get("download", {}).get("package"), a.get("version")) for a in registry["apps"]}
                remaining = versions & withdrawn
                if remaining:
                    errors.append(f"{url}: withdrawn versions still listed: {sorted(remaining)}")
            except (OSError, ValueError) as exc:
                errors.append(f"{url}: {exc}")
        if not errors:
            print("Both store registries exclude withdrawn versions")
            return
        print("; ".join(errors), flush=True)
        if attempt + 1 < attempts:
            time.sleep(30)
    raise ValueError("registry withdrawal verification did not complete")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("plan")
    lookup = commands.add_parser("lookup")
    lookup.add_argument("path")
    prune = commands.add_parser("prune-tree")
    prune.add_argument("root", type=Path)
    release = commands.add_parser("releases")
    release.add_argument("plan", type=Path)
    release.add_argument("--dry-run", action="store_true")
    verify = commands.add_parser("verify-registries")
    verify.add_argument("plan", type=Path)
    verify.add_argument("urls", nargs="+")
    args = parser.parse_args()
    if args.command == "plan":
        print(json.dumps(plan(), indent=2))
    elif args.command == "lookup":
        if not MANIFEST.fullmatch(args.path):
            raise ValueError("invalid manifest path")
        data = historical_manifest(args.path)
        if not data:
            raise ValueError("version was not previously withdrawn")
        print(json.dumps(data))
    elif args.command == "prune-tree":
        prune_tree(args.root)
    elif args.command == "releases":
        release_cleanup(json.loads(args.plan.read_text()), args.dry_run)
    elif args.command == "verify-registries":
        verify_registries(json.loads(args.plan.read_text()), args.urls)


if __name__ == "__main__":
    main()
