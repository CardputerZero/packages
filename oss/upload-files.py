"""Compare and upload pool files concurrently, then publish repository metadata."""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
import os
from pathlib import Path
import re
from oss_command import command


def parse_inventory(listing, prefix):
    """Parse the complete ossutil 1.7.7 long listing, including spaces in keys."""
    objects = {}
    counts = []
    for line in listing.splitlines():
        count = re.fullmatch(r"Object Number is: (\d+)", line)
        if count:
            counts.append(int(count[1]))
        elif not line or re.fullmatch(r"\d+(?:\.\d+)?\(s\) elapsed", line):
            continue
        elif re.fullmatch(r"LastModifiedTime\s+Size\(B\)\s+StorageClass\s+ETAG\s+ObjectName", line):
            continue
        else:
            row = re.fullmatch(r"(\d{4}-\d\d-\d\d\s+\d\d:\d\d:\d\d\s+[+-]\d{4}\s+\S+)\s+(\d+)\s+\S+\s+(\S+)\s+(oss://.+)", line)
            if not row or not row[4].startswith(prefix) or row[4] in objects:
                raise ValueError("Incomplete or ambiguous OSS inventory")
            objects[row[4]] = [row[1], int(row[2]), row[3].strip('"').lower()]
    if counts != [len(objects)]:
        raise ValueError("Incomplete or ambiguous OSS inventory")
    return objects


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_state(path, target):
    if not path:
        return {}
    try:
        data = json.loads(path.read_text())
        if data.get("schema") == 1 and data.get("target") == target and isinstance(data.get("files"), dict):
            return data["files"]
    except (OSError, ValueError, AttributeError):
        pass
    return {}


def save_state(path, target, files):
    if path:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps({"schema": 1, "target": target, "files": files}, sort_keys=True))
        temp.replace(path)


def header(output, name):
    match = re.search(r"^" + re.escape(name) + r"\s*:\s*([^\r\n]+?)\s*$", output, re.I | re.M)
    return match[1].strip('"') if match else None


def sync_file(root, destination, path, inventory=None, state=None):
    relative = path.relative_to(root).as_posix()
    remote = f'{destination}/{relative}'
    identity = inventory.get(remote) if inventory is not None else None
    digest = sha256(path) if inventory is not None else None
    if identity and state.get(relative) == {"sha256": digest, "object": identity}:
        print(f'Verified unchanged: {relative}', flush=True)
        return 0, state[relative], True
    try:
        stat = command('stat', remote)
    except RuntimeError as exc:
        if 'StatusCode=404,' not in str(exc):
            raise
    else:
        remote_crc = re.search(r'^x-oss-hash-crc64ecma\s*:\s*(\d+)\s*$', stat, re.I | re.M)
        if remote_crc:
            local_hash = command('hash', str(path), '--type=crc64')
            local_crc = re.search(r'^CRC64-ECMA\s*:\s*(\d+)\s*$', local_hash, re.M)
            if local_crc and local_crc[1] == remote_crc[1]:
                print(f'Unchanged: {relative}', flush=True)
                # ETag identifies the object, but is not necessarily MD5. Only
                # persist identities that were actually verified by CRC64.
                record = None
                if (identity and identity[1] == path.stat().st_size
                        and identity[2] == (header(stat, "ETag") or "").lower()
                        and header(stat, "Content-Length") == str(identity[1])):
                    record = {"sha256": digest, "object": identity}
                return 0, record, False
    print(f'Uploading {relative} -> {remote}', flush=True)
    command('cp', '-f', str(path), remote)
    print(f'Uploaded: {relative}', flush=True)
    # The next run verifies newly uploaded data by CRC before caching it.
    return 1, None, False


def sync(root, destination, workers=8, state_path=None):
    if not 1 <= workers <= 32:
        raise ValueError('OSS_SYNC_WORKERS must be between 1 and 32')
    files = sorted(path for path in root.rglob('*') if path.is_file())
    pool = [path for path in files if path.relative_to(root).parts[0] == 'pool']
    metadata = [path for path in files if path not in pool]
    target = [os.environ.get("OSS_ENDPOINT", ""), destination]
    state = load_state(state_path, target)
    inventory = None
    if state_path:
        prefix = f"{destination}/pool/"
        try:
            inventory = parse_inventory(command("ls", prefix), prefix)
        except (RuntimeError, ValueError) as exc:
            # A cache miss or an unavailable optimization must not publish
            # unverified objects, nor disable the existing CRC fallback.
            print(f"OSS inventory unavailable; using per-object checks: {exc}", flush=True)
        else:
            print(f"OSS inventory: {len(inventory)} objects; {len(state)} previously verified files", flush=True)
    verified = {}
    cached = 0
    uploaded = 0
    print(f'Checking OSS pool with {workers} workers', flush=True)
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(sync_file, root, destination, path, inventory, state): path for path in pool}
        try:
            for future in as_completed(futures):
                count, record, hit = future.result()
                uploaded += count
                cached += hit
                if record:
                    verified[futures[future].relative_to(root).as_posix()] = record
        except Exception:
            for future in futures:
                future.cancel()
            raise
    # Every binary and image must succeed before changing any catalog. Packages
    # precede Release, and the store registry is the last publication step.
    def metadata_order(path):
        relative = path.relative_to(root).as_posix()
        return (2 if relative == 'cn/registry.json' else 1 if relative == 'dists/stable/Release' else 0, relative)
    for path in sorted(metadata, key=metadata_order):
        count, _, _ = sync_file(root, destination, path)
        uploaded += count
    save_state(state_path, target, verified)
    print(f"OSS pool verified cache hits: {cached}/{len(pool)}", flush=True)
    print(f'OSS sync complete: {uploaded} files uploaded, {len(files) - uploaded} unchanged at {destination}/', flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('root', type=Path)
    parser.add_argument('destination')
    parser.add_argument('--workers', type=int, default=os.environ.get('OSS_SYNC_WORKERS', '8'))
    parser.add_argument('--state', type=Path, default=os.environ.get('OSS_SYNC_STATE'))
    args = parser.parse_args()
    sync(args.root, args.destination, args.workers, args.state)
