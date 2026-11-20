#!/usr/bin/env python3
"""Select latest store versions and reconcile withdrawals from merged main."""
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
    commit = git("log", "--first-parent", "--no-renames", "-1", "--diff-filter=D", "--format=%H", "HEAD", "--", path)
    if not commit:
        return None
    return json.loads(git("show", f"{commit}^:{path}"))


def compare_versions(left, right):
    for relation, result in (("gt", 1), ("lt", -1)):
        code = subprocess.run(["dpkg", "--compare-versions", left, relation, right]).returncode
        if code == 0:
            return result
        if code != 1:
            raise ValueError(f"invalid Debian versions: {left!r}, {right!r}")
    return 0


def current_manifests():
    result = {}
    for path in sorted(Path("pool/main").glob("*/*.deb.release.json")):
        match = MANIFEST.fullmatch(path.as_posix())
        if not match:
            raise ValueError(f"invalid manifest path: {path}")
        data = json.loads(path.read_text())
        if data["filename"] != match[2] or data["package"] != match[1]:
            raise ValueError(f"manifest identity mismatch: {path}")
        # Validate even the only version of a package before changing anything.
        subprocess.run(["dpkg", "--validate-version", data["version"]], check=True)
        result[data["filename"]] = data
    return result


def split_latest(manifests):
    """One Debian version per package; keep both architectures at that version."""
    latest = {}
    for data in manifests.values():
        pkg, version = data["package"], data["version"]
        if pkg not in latest or compare_versions(version, latest[pkg]) > 0:
            latest[pkg] = version
    active, superseded = {}, {}
    for name, data in manifests.items():
        target = active if compare_versions(data["version"], latest[data["package"]]) == 0 else superseded
        target[name] = data
    return active, superseded


def supersede(package, version):
    """Stage old-manifest deletions in a publish PR, leaving listing assets alone."""
    manifests = current_manifests()
    if not any(m["package"] == package and compare_versions(m["version"], version) == 0
               for m in manifests.values()):
        raise ValueError("replacement manifest is missing")
    obsolete = [m for m in manifests.values() if m["package"] == package
                and compare_versions(m["version"], version) < 0]
    for item in obsolete:
        manifest_path(item).unlink()


def manifest_path(item):
    path = f"pool/main/{item['package']}/{item['filename']}.release.json"
    if not MANIFEST.fullmatch(path):
        raise ValueError(f"invalid manifest path: {path}")
    return Path(path)


def stage_superseded(data):
    for item in data.get("superseded", []):
        path = manifest_path(item)
        if json.loads(path.read_text()) != item:
            raise ValueError(f"manifest changed since planning: {path}")
    for item in data.get("superseded", []):
        manifest_path(item).unlink()


def retire_superseded(data, attempts=3):
    """Persist only still-obsolete snapshot manifests after successful publication.

    Each push is a fast-forward, so a concurrent publisher cannot be overwritten.
    Failed attempts re-read current main and recheck both the old identity and
    the presence of a newer replacement before committing deletions.
    """
    if not data.get("superseded"):
        return
    for _ in range(attempts):
        subprocess.run(["git", "fetch", "origin", "main"], check=True)
        subprocess.run(["git", "checkout", "--detach", "FETCH_HEAD"], check=True)
        _, obsolete = split_latest(current_manifests())
        paths = [manifest_path(m) for m in data["superseded"]
                 if obsolete.get(m["filename"]) == m]
        if not paths:
            return
        for path in paths:
            path.unlink()
        subprocess.run(["git", "add", "--", *map(str, paths)], check=True)
        subprocess.run(["git", "-c", "user.name=cardputerzero-bot", "-c",
                        "user.email=cardputerzero-bot@users.noreply.github.com",
                        "commit", "-m", "Retire superseded package versions after deployment"], check=True)
        if subprocess.run(["git", "push", "origin", "HEAD:refs/heads/main"]).returncode == 0:
            return
    raise RuntimeError("main advanced during version retirement; retry deployment")


def plan():
    if git("rev-parse", "--is-shallow-repository") != "false":
        raise ValueError("full main history is required for withdrawal cleanup")
    active, superseded = split_latest(current_manifests())
    # Version replacements must expose the old path as a deletion even when
    # Git considers the two manifests similar enough to be a rename.
    deleted = git("log", "--first-parent", "--no-renames", "--format=", "--name-only", "--diff-filter=D", "HEAD", "--", "pool/main")
    withdrawn = []
    for path in sorted(set(deleted.splitlines())):
        match = MANIFEST.fullmatch(path)
        if not match or match[2] in active or match[2] in superseded:
            continue
        data = historical_manifest(path)
        if not data or data["filename"] != match[2] or data["package"] != match[1]:
            raise ValueError(f"invalid withdrawn manifest: {path}")
        withdrawn.append(data)
    cn_excluded = []
    for package in sorted({m["package"] for m in active.values()}):
        meta = Path("pool/main") / package / "meta.json"
        if meta.is_file():
            region = json.loads(meta.read_text()).get("region")
            if isinstance(region, dict):
                value = region.get("CN")
                if value is False or (isinstance(value, str) and value.lower() == "false"):
                    cn_excluded.append(package)
    return {"sha": git("rev-parse", "HEAD"), "active": list(active.values()),
            "superseded": list(superseded.values()), "withdrawn": withdrawn + list(superseded.values()),
            "cn_excluded": cn_excluded}


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


def verify_registries(data, urls, attempts=20, max_wait=180, interval=10):
    withdrawn = {(m["package"], m["version"]) for m in data["withdrawn"]}
    active = {(m["package"], m["version"]) for m in data["active"]}
    withdrawn -= active
    cn_excluded = set(data.get("cn_excluded", []))
    deadline = time.monotonic() + max_wait
    for attempt in range(attempts):
        errors = []
        for position, url in enumerate(urls):
            try:
                remaining_time = deadline - time.monotonic()
                if remaining_time <= 0:
                    raise TimeoutError("verification deadline reached")
                separator = "&" if "?" in url else "?"
                req = urllib.request.Request(url + separator + "withdrawal_check=" + str(time.time_ns()),
                                             headers={"Cache-Control": "no-cache", "User-Agent": "cz-withdrawal-check"})
                with urllib.request.urlopen(req, timeout=min(15, remaining_time)) as response:
                    registry = json.load(response)
                if not isinstance(registry.get("apps"), list):
                    raise ValueError("invalid registry schema")
                versions = {(a.get("download", {}).get("package"), a.get("version")) for a in registry["apps"]}
                remaining = versions & withdrawn
                if remaining:
                    errors.append(f"{url}: withdrawn versions still listed: {sorted(remaining)}")
                expected = active if position == 0 else {(p, v) for p, v in active if p not in cn_excluded}
                missing = expected - versions
                if missing:
                    errors.append(f"{url}: published versions missing: {sorted(missing)}")
                unexpected = versions - expected
                if unexpected:
                    errors.append(f"{url}: unexpected versions: {sorted(unexpected)}")
            except (OSError, ValueError, AttributeError, TypeError) as exc:
                errors.append(f"{url}: {exc}")
        if not errors:
            print("Both store registries exclude withdrawn versions and contain replacements")
            return
        print(f"Registry verification attempt {attempt + 1}: " + "; ".join(errors), flush=True)
        remaining_time = deadline - time.monotonic()
        if attempt + 1 >= attempts or remaining_time <= 0:
            break
        time.sleep(min(interval, remaining_time))
    raise ValueError("registry withdrawal verification did not complete")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("plan")
    replace = commands.add_parser("supersede")
    replace.add_argument("package")
    replace.add_argument("version")
    for command in ("stage-superseded", "retire-superseded"):
        action = commands.add_parser(command)
        action.add_argument("plan", type=Path)
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
    elif args.command == "supersede":
        supersede(args.package, args.version)
    elif args.command == "stage-superseded":
        stage_superseded(json.loads(args.plan.read_text()))
    elif args.command == "retire-superseded":
        retire_superseded(json.loads(args.plan.read_text()))
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
