"""Compare and upload pool files concurrently, then publish repository metadata."""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import os
from pathlib import Path
import re
import subprocess


def command(*args):
    result = subprocess.run(['ossutil', *args], text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if result.returncode:
        raise RuntimeError(result.stdout.strip() or f'ossutil {args[0]} failed')
    return result.stdout


def sync_file(root, destination, path):
    relative = path.relative_to(root).as_posix()
    remote = f'{destination}/{relative}'
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
                return 0
    print(f'Uploading {relative} -> {remote}', flush=True)
    command('cp', '-f', str(path), remote)
    return 1


def sync(root, destination, workers=8):
    if not 1 <= workers <= 32:
        raise ValueError('OSS_SYNC_WORKERS must be between 1 and 32')
    files = sorted(path for path in root.rglob('*') if path.is_file())
    pool = [path for path in files if path.relative_to(root).parts[0] == 'pool']
    metadata = [path for path in files if path not in pool]
    uploaded = 0
    print(f'Checking OSS pool with {workers} workers', flush=True)
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(sync_file, root, destination, path) for path in pool]
        try:
            for future in as_completed(futures):
                uploaded += future.result()
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
        uploaded += sync_file(root, destination, path)
    print(f'OSS sync complete: {uploaded} files uploaded, {len(files) - uploaded} unchanged at {destination}/', flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('root', type=Path)
    parser.add_argument('destination')
    parser.add_argument('--workers', type=int, default=os.environ.get('OSS_SYNC_WORKERS', '8'))
    args = parser.parse_args()
    sync(args.root, args.destination, args.workers)
