"""Verified package caches, promotion sources, reuse and bounded downloads."""
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
import urllib.error

SCRIPTS = Path(__file__).resolve().parents[1] / '.github/scripts'
sys.path.insert(0, str(SCRIPTS))
import collect_packages as packages


class Response(io.BytesIO):
    def __init__(self, content):
        super().__init__(content)
        self.headers = {'Content-Length': str(len(content))}


class PackageTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.pool = self.root / 'pool'
        self.cache = self.root / 'cache'
        self.cache.mkdir()
        self.output = self.root / 'release-debs'
        self.content = b'valid package'
        self.item = self.manifest('sample', self.content)
        self.cached = self.cache / (self.item['sha256'] + '.deb')

    def manifest(self, name, content):
        filename = f'{name}_1.0_arm64.deb'
        directory = self.pool / name
        directory.mkdir(parents=True)
        data = {'filename': filename, 'sha256': hashlib.sha256(content).hexdigest(),
                'url': f'https://github.com/owner/repo/releases/download/staging/developer__{filename}'}
        (directory / f'{filename}.release.json').write_text(json.dumps(data))
        return data

    def collect(self, existing=True):
        assets = [dict(name=self.item['filename'])] if existing else []
        with patch.object(packages.subprocess, 'check_output', return_value=json.dumps(dict(assets=assets))):
            packages.collect(self.pool, self.output, self.cache, 'owner/repo')

    def test_verified_cache_skips_download_and_prunes_inactive_files(self):
        self.cached.write_bytes(self.content)
        (self.cache / 'withdrawn.deb').write_bytes(b'old')
        self.output.mkdir()
        (self.output / 'withdrawn.deb').write_bytes(b'old')
        with patch.object(packages, 'download') as fetch:
            self.collect()
        fetch.assert_not_called()
        self.assertEqual(list(self.cache.iterdir()), [self.cached])
        self.assertEqual([p.name for p in self.output.iterdir()], [self.item['filename']])
        self.assertEqual((self.output / self.item['filename']).read_bytes(), self.content)

    def test_corrupt_cache_is_replaced_using_manifest_checksum(self):
        self.cached.write_bytes(b'corrupt')
        def fetch(url, destination, sha256, **kwargs):
            self.assertEqual(sha256, self.item['sha256'])
            self.assertIn('/apt-pool/', url)
            destination.write_bytes(self.content)
        with patch.object(packages, 'download', side_effect=fetch) as request:
            self.collect()
        self.assertEqual(request.call_count, 1)
        self.assertEqual(self.cached.read_bytes(), self.content)

    def test_new_package_uses_prefixed_source_and_reuses_download_for_collection(self):
        def fetch(url, destination, sha256, **kwargs):
            self.assertEqual(url, self.item['url'])
            self.assertFalse(kwargs['retry_not_found'])
            destination.write_bytes(self.content)
        with patch.object(packages, 'download', side_effect=fetch) as request, \
             patch.object(packages.subprocess, 'run') as upload:
            self.collect(existing=False)
        self.assertEqual(request.call_count, 1)
        self.assertEqual(upload.call_args.args[0][4], str(self.output / self.item['filename']))
        self.assertEqual((self.output / self.item['filename']).read_bytes(), self.content)

    def test_cached_new_package_is_still_promoted(self):
        self.cached.write_bytes(self.content)
        with patch.object(packages, 'download') as request, patch.object(packages.subprocess, 'run') as upload:
            self.collect(existing=False)
        request.assert_not_called()
        upload.assert_called_once()

    def test_fork_fallback_is_immediate_after_missing_org_mirror(self):
        self.item['url'] = 'https://github.com/dev/fork/releases/download/submission/sample.deb'
        missing = urllib.error.HTTPError('url', 404, 'missing', {}, None)
        with patch('urllib.request.urlopen',
                          side_effect=[missing, Response(self.content)]) as request, \
             patch.object(packages.time, 'sleep') as sleep:
            packages.fetch_sources(self.item, self.cached, 'owner/repo')
        self.assertEqual(request.call_count, 2)
        sleep.assert_not_called()
        self.assertEqual(self.cached.read_bytes(), self.content)

    def test_bad_source_checksum_is_never_collected_or_uploaded(self):
        with patch('urllib.request.urlopen', side_effect=lambda *a, **k: Response(b'bad')), \
             patch.object(packages.subprocess, 'run') as upload, self.assertRaisesRegex(RuntimeError, 'matching sha256'):
            self.collect(existing=False)
        upload.assert_not_called()
        self.assertEqual(list(self.cache.iterdir()), [])
        self.assertEqual(list(self.output.iterdir()), [])

    def test_downloads_are_concurrent_and_bounded(self):
        items = [self.item] + [self.manifest(f'sample{i}', f'package{i}'.encode()) for i in range(3)]
        bodies = {self.item['sha256']: self.content}
        bodies.update({x['sha256']: f'package{i}'.encode() for i, x in enumerate(items[1:])})
        barrier = threading.Barrier(4)
        def fetch(url, destination, sha256, **kwargs):
            barrier.wait(timeout=5)
            destination.write_bytes(bodies[sha256])
        with patch.object(packages, 'download', side_effect=fetch), \
             patch.object(packages.subprocess, 'check_output', return_value=json.dumps({'assets': [{'name': x['filename']} for x in items]})):
            packages.collect(self.pool, self.output, self.cache, 'owner/repo', workers=4)
        self.assertEqual(len(list(self.output.iterdir())), 4)

    def test_identical_package_contents_share_one_download(self):
        second = self.manifest('alias', self.content)
        def fetch(url, destination, sha256, **kwargs):
            destination.write_bytes(self.content)
        with patch.object(packages, 'download', side_effect=fetch) as request, \
             patch.object(packages.subprocess, 'check_output', return_value=json.dumps({'assets': [
                 {'name': self.item['filename']}, {'name': second['filename']}]})):
            packages.collect(self.pool, self.output, self.cache, 'owner/repo')
        self.assertEqual(request.call_count, 1)
        self.assertEqual(len(list(self.output.iterdir())), 2)

    def test_empty_catalog_cannot_restore_withdrawn_cached_packages(self):
        next(self.pool.rglob('*.release.json')).unlink()
        self.cached.write_bytes(self.content)
        with patch.object(packages, 'download') as request:
            self.collect()
        request.assert_not_called()
        self.assertEqual(list(self.cache.iterdir()), [])
        self.assertEqual(list(self.output.iterdir()), [])

    def test_invalid_manifest_cannot_escape_output_directory(self):
        path = next(self.pool.rglob('*.release.json'))
        path.write_text(json.dumps({**self.item, 'filename': '../escape.deb'}))
        with patch.object(packages.subprocess, 'check_output') as release, self.assertRaises(ValueError):
            self.collect()
        release.assert_not_called()


if __name__ == '__main__':
    unittest.main()
