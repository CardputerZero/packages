#!/usr/bin/env python3
"""Promote and collect exactly the manifested packages with verified local caching."""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
import threading
from urllib.parse import quote

from download import download


def checksum(path):
    digest = hashlib.sha256()
    with path.open('rb') as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def manifests(root):
    result = {}
    for path in sorted(root.rglob('*.deb.release.json')):
        item = json.loads(path.read_text())
        name = item.get('filename', '')
        sha = item.get('sha256', '')
        if not isinstance(name, str) or '/' in name or '\\' in name or not name.endswith('.deb'):
            raise ValueError(f'Invalid package filename in {path}')
        if not isinstance(sha, str) or not re.fullmatch(r'[a-fA-F0-9]{64}', sha):
            raise ValueError(f'Missing or invalid sha256 in {path}')
        item['sha256'] = sha.lower()
        if name in result:
            raise ValueError(f'Duplicate package filename: {name}')
        result[name] = item
    return result


def sources(item, repository):
    staging = f'https://github.com/{repository}/releases/download/staging/'
    mirror = staging + quote(item['filename'], safe='')
    original = item.get('url', '')
    # Portal submissions already name the org-controlled, author-prefixed asset.
    # Other submissions still prefer the org mirror over a contributor's release.
    candidates = [original, mirror] if original.startswith(staging) else [mirror, original]
    return list(dict.fromkeys(url for url in candidates if url))


def fetch_sources(item, output, repository, budget=600):
    deadline = time.monotonic() + budget
    errors = []
    for url in sources(item, repository):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        print(f"Fetching {item['filename']} from {url}", flush=True)
        try:
            download(url, output, item['sha256'], budget=remaining, retry_not_found=False)
            return
        except (OSError, ValueError, http.client.HTTPException) as exc:
            errors.append(f'{url}: {exc}')
    raise RuntimeError(f"Could not fetch {item['filename']} with a matching sha256: {'; '.join(errors)}")


def collect_one(item, pool_assets, repository, tag, cache, output, cache_lock):
    name = item['filename']
    cached = cache / f"{item['sha256']}.deb"
    destination = output / name
    # Different asset names can legitimately contain the same bytes. Serialize
    # access to that shared cache entry while unrelated packages run in parallel.
    with cache_lock:
        valid = cached.is_file() and checksum(cached) == item['sha256']
        if not valid:
            cached.unlink(missing_ok=True)
            if name in pool_assets:
                url = f'https://github.com/{repository}/releases/download/{quote(tag, safe="")}/{quote(name, safe="")}'
                print(f'Downloading from pool: {name}', flush=True)
                download(url, cached, item['sha256'])
            else:
                fetch_sources(item, cached, repository)
        else:
            print(f'Cached: {name}', flush=True)
        shutil.copyfile(cached, destination)
    if name not in pool_assets:
        # Upload the canonical basename, even though the cache is keyed by SHA256.
        subprocess.run(['gh', 'release', 'upload', tag, str(destination), '--clobber', '--repo', repository], check=True)
        print(f'Promoted: {name}', flush=True)
    return valid


def collect(root, output, cache, repository, tag='apt-pool', workers=4):
    if not 1 <= workers <= 16:
        raise ValueError('Package download workers must be between 1 and 16')
    items = manifests(root)
    assets = json.loads(subprocess.check_output(
        ['gh', 'release', 'view', tag, '--repo', repository, '--json', 'assets'], text=True))
    pool_assets = {asset['name'] for asset in assets['assets']}
    output.mkdir(parents=True, exist_ok=True)
    cache.mkdir(parents=True, exist_ok=True)
    # Only active manifests determine the catalog and the next saved cache.
    for path in output.glob('*.deb'):
        path.unlink()
    wanted = {f"{item['sha256']}.deb" for item in items.values()}
    for path in cache.glob('*.deb'):
        if path.name not in wanted:
            path.unlink()
    hits = 0
    cache_locks = {item['sha256']: threading.Lock() for item in items.values()}
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(collect_one, item, pool_assets, repository, tag, cache, output, cache_locks[item['sha256']])
                   for item in items.values()]
        try:
            for future in as_completed(futures):
                hits += future.result()
        except Exception:
            for future in futures:
                future.cancel()
            raise
    print(f'Collected {len(items)} .deb from manifests: {hits} cache hits, {len(items) - hits} downloads', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path('pool/main'))
    parser.add_argument('--output', type=Path, default=Path('release-debs'))
    parser.add_argument('--cache', type=Path, default=Path('.cache/package-debs'))
    parser.add_argument('--repository', default=os.environ.get('GITHUB_REPOSITORY'))
    parser.add_argument('--tag', default=os.environ.get('POOL_TAG', 'apt-pool'))
    parser.add_argument('--workers', type=int, default=4)
    args = parser.parse_args()
    if not args.repository:
        parser.error('--repository or GITHUB_REPOSITORY is required')
    collect(args.root, args.output, args.cache, args.repository, args.tag, args.workers)


if __name__ == '__main__':
    main()
