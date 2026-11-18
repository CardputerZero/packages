"""Regression coverage for withdrawal, upload retries and first-release gating."""
import contextlib
import hashlib
import importlib.util
import io
import json
import os
import re
import runpy
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest.mock import patch
import urllib.error

ROOT = Path(__file__).resolve().parents[1]


def module(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / '.github/scripts' / f'{name}.py')
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


download = module('download')
cleanup = module('catalog_cleanup')


class Response(io.BytesIO):
    def __init__(self, data, length=None):
        super().__init__(data)
        self.headers = {'Content-Length': str(len(data) if length is None else length)}


class DownloadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.output = Path(self.tmp.name) / 'package.deb'

    def test_504_then_success_and_retry_backoff(self):
        errors = [urllib.error.HTTPError('https://example.test', 504, '', {}, None) for _ in range(3)]
        with patch.object(download.urllib.request, 'urlopen', side_effect=errors + [Response(b'valid')]) as request, \
             patch.object(download.time, 'sleep') as sleep:
            download.download('https://example.test', self.output)
        self.assertEqual(request.call_count, 4)
        self.assertEqual([x.args[0] for x in sleep.call_args_list], [15, 30, 60])
        self.assertEqual(self.output.read_bytes(), b'valid')

    def test_partial_download_is_retried_without_exposing_partial_output(self):
        with patch.object(download.urllib.request, 'urlopen', side_effect=[Response(b'partial', 100), Response(b'complete')]), \
             patch.object(download.time, 'sleep'):
            download.download('https://example.test', self.output)
        self.assertEqual(self.output.read_bytes(), b'complete')
        self.assertEqual(list(Path(self.tmp.name).iterdir()), [self.output])

    def test_checksum_failure_is_not_retried(self):
        with patch.object(download.urllib.request, 'urlopen', return_value=Response(b'bad')) as request, \
             self.assertRaisesRegex(ValueError, 'checksum mismatch'):
            download.download('https://example.test', self.output, hashlib.sha256(b'good').hexdigest())
        self.assertEqual(request.call_count, 1)
        self.assertFalse(self.output.exists())
        self.assertEqual(list(Path(self.tmp.name).iterdir()), [])

    def test_permanent_http_error_is_not_retried(self):
        error = urllib.error.HTTPError('https://example.test', 403, '', {}, None)
        with patch.object(download.urllib.request, 'urlopen', side_effect=error) as request, self.assertRaises(urllib.error.HTTPError):
            download.download('https://example.test', self.output)
        self.assertEqual(request.call_count, 1)

    def test_optional_mirror_404_is_not_retried(self):
        error = urllib.error.HTTPError('https://example.test', 404, '', {}, None)
        with patch.object(download.urllib.request, 'urlopen', side_effect=error) as request, \
             patch.object(download.time, 'sleep') as sleep, self.assertRaises(urllib.error.HTTPError):
            download.download('https://example.test', self.output, retry_not_found=False)
        self.assertEqual(request.call_count, 1)
        sleep.assert_not_called()

    def test_cli_downloads_once_and_accepts_sha256(self):
        sha = hashlib.sha256(b'valid').hexdigest()
        with patch('urllib.request.urlopen', return_value=Response(b'valid')) as request, \
             patch.object(sys, 'argv', ['download.py', 'https://example.test', str(self.output), '--sha256', sha]), \
             self.assertRaises(SystemExit) as stopped:
            runpy.run_path(str(ROOT / '.github/scripts/download.py'), run_name='__main__')
        self.assertEqual(stopped.exception.code, 0)
        self.assertEqual(request.call_count, 1)
        self.assertEqual(self.output.read_bytes(), b'valid')

    def test_retry_exhaustion_keeps_existing_file_intact(self):
        self.output.write_bytes(b'previous complete file')
        with patch.object(download.urllib.request, 'urlopen', side_effect=TimeoutError), \
             patch.object(download.time, 'sleep'), self.assertRaises(TimeoutError):
            download.download('https://example.test', self.output)
        self.assertEqual(self.output.read_bytes(), b'previous complete file')
        self.assertEqual(list(Path(self.tmp.name).iterdir()), [self.output])


class WithdrawalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.old_cwd = Path.cwd()
        os.chdir(self.tmp.name)
        self.addCleanup(os.chdir, self.old_cwd)
        self.git('init', '-q')
        self.git('config', 'user.email', 'tests@example.test')
        self.git('config', 'user.name', 'Tests')
        self.git('config', 'commit.gpgsign', 'false')
        self.old = self.manifest('sample', '1.0')
        self.current = self.manifest('sample', '2.0')
        self.commit('publish both versions')

    def git(self, *args):
        return subprocess.check_output(['git', *args], text=True).strip()

    def commit(self, message):
        self.git('add', '-A')
        self.git('commit', '-qm', message)

    def manifest(self, package, version):
        path = Path(f'pool/main/{package}/{package}_{version}_arm64.deb.release.json')
        path.parent.mkdir(parents=True, exist_ok=True)
        data = dict(package=package, version=version, filename=path.name.removesuffix('.release.json'), uploaded_by='owner')
        path.write_text(json.dumps(data))
        return path

    def test_deleted_version_is_planned_but_other_version_is_kept(self):
        self.old.unlink()
        self.commit('unpublish old version')
        data = cleanup.plan()
        self.assertEqual([m['version'] for m in data['withdrawn']], ['1.0'])
        self.assertEqual([m['version'] for m in data['active']], ['2.0'])
        self.assertEqual(cleanup.historical_manifest(str(self.old))['uploaded_by'], 'owner')

    def test_republished_filename_is_never_deleted(self):
        self.old.unlink()
        self.commit('unpublish old version')
        self.manifest('sample', '1.0')
        self.commit('republish intentionally')
        self.assertEqual(cleanup.plan()['withdrawn'], [])

    def test_unknown_version_is_not_treated_as_already_removed(self):
        self.assertIsNone(cleanup.historical_manifest('pool/main/unknown/unknown_1_arm64.deb.release.json'))

    def test_prune_metadata_only_after_last_version(self):
        orphan = Path('pool/main/retired')
        orphan.mkdir()
        (orphan / 'meta.json').write_text('{}')
        cleanup.prune_tree(Path('pool/main'))
        self.assertFalse(orphan.exists())
        self.assertTrue(self.current.exists())

    def test_release_cleanup_is_exact_paginated_and_idempotent(self):
        data = {'sha': 'current', 'active': [{'filename': 'sample_2.0_arm64.deb', 'package': 'sample'}],
                'withdrawn': [{'filename': 'sample_1.0_arm64.deb', 'package': 'sample', 'uploaded_by': 'owner'},
                              {'filename': 'retired_1.0_arm64.deb', 'package': 'retired', 'uploaded_by': 'owner'}]}
        releases = [[{'id': 1, 'tag_name': 'apt-pool'}, {'id': 2, 'tag_name': 'staging'}]]
        assets = {1: [[{'id': 10, 'name': 'sample_1.0_arm64.deb'}], [{'id': 11, 'name': 'sample_2.0_arm64.deb'}]],
                  2: [[{'id': 12, 'name': 'owner__sample_1.0_arm64.deb'}, {'id': 13, 'name': 'sample_3.0_arm64.deb'},
                       {'id': 14, 'name': 'owner__sample__icon.png'}, {'id': 15, 'name': 'owner__retired__shot0.png'},
                       {'id': 16, 'name': 'other__retired__shot0.png'}]]}
        def api(cmd, **kwargs):
            match = re.search(r'/releases/(\d+)/assets\?', cmd[-1])
            return json.dumps(assets[int(match[1])] if match else releases)
        def remove(cmd, **kwargs):
            asset_id = int(cmd[-1].rsplit('/', 1)[-1])
            for pages in assets.values():
                for page in pages:
                    page[:] = [a for a in page if a['id'] != asset_id]
        with patch.dict(os.environ, {'GITHUB_REPOSITORY': 'owner/repo'}), \
             patch.object(cleanup, 'gh_json', return_value={'sha': 'current'}), \
             patch.object(cleanup.subprocess, 'check_output', side_effect=api), \
             patch.object(cleanup.subprocess, 'run', side_effect=remove) as delete, contextlib.redirect_stdout(io.StringIO()):
            cleanup.release_cleanup(data)
            cleanup.release_cleanup(data)
        paths = [c.args[0][-1] for c in delete.call_args_list]
        self.assertEqual(paths, ['repos/owner/repo/releases/assets/10', 'repos/owner/repo/releases/assets/12',
                                 'repos/owner/repo/releases/assets/15'])

    def test_first_release_comment_handles_a_large_tree_without_sigpipe(self):
        for i in range(1000):
            path = Path(f'pool/main/zz-later/file-{i:04d}.png')
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch()
        self.commit('add enough paths to exceed the pipe buffer')
        text = (ROOT / '.github/workflows/validate-pr.yml').read_text()
        condition = re.search(r'if ! (git ls-tree[^\n]+); then\n\s+NEW_PKGS=', text)[1]
        script = 'set -o pipefail\nBASE_SHA=HEAD\ntp=sample\nif ! ' + condition + '; then echo new; else echo update; fi'
        self.assertEqual(subprocess.check_output(['bash', '-c', script], text=True).strip(), 'update')
        self.old.unlink()
        self.current.unlink()
        self.commit('remove all versions but keep metadata')
        self.assertEqual(subprocess.check_output(['bash', '-c', script], text=True).strip(), 'new')

    def test_truncated_current_tree_refuses_to_delete_assets(self):
        with patch.dict(os.environ, {'GITHUB_REPOSITORY': 'owner/repo'}), \
             patch.object(cleanup, 'gh_json', side_effect=[{'sha': 'newer'}, {'truncated': True}]), \
             patch.object(cleanup.subprocess, 'run') as delete, self.assertRaisesRegex(ValueError, 'truncated'):
            cleanup.release_cleanup({'sha': 'old', 'active': []})
        delete.assert_not_called()

    def test_republished_version_on_newer_main_is_protected(self):
        data = {'sha': 'old', 'active': [], 'withdrawn': [{'filename': 'sample_1.0_arm64.deb'}]}
        tree = {'truncated': False, 'tree': [{'type': 'blob', 'path': 'pool/main/sample/sample_1.0_arm64.deb.release.json'}]}
        pages = [[{'id': 1, 'tag_name': 'apt-pool'}]]
        assets = [[{'id': 10, 'name': 'sample_1.0_arm64.deb'}]]
        with patch.dict(os.environ, {'GITHUB_REPOSITORY': 'owner/repo'}), \
             patch.object(cleanup, 'gh_json', side_effect=[{'sha': 'newer'}, tree]), \
             patch.object(cleanup.subprocess, 'check_output', side_effect=[json.dumps(pages), json.dumps(assets)]), \
             patch.object(cleanup.subprocess, 'run') as delete:
            cleanup.release_cleanup(data)
        delete.assert_not_called()

    def test_registry_verification_checks_package_and_version(self):
        data = {'withdrawn': [{'package': 'sample', 'version': '1.0'}],
                'active': [{'package': 'sample', 'version': '2.0'}]}
        registry = {'apps': [{'version': '2.0', 'download': {'package': 'sample'}}]}
        with patch.object(cleanup.urllib.request, 'urlopen', side_effect=lambda *a, **k: Response(json.dumps(registry).encode())), \
             contextlib.redirect_stdout(io.StringIO()):
            cleanup.verify_registries(data, ['https://global.test', 'https://cn.test'], attempts=1)
        registry['apps'].append({'version': '1.0', 'download': {'package': 'sample'}})
        with patch.object(cleanup.urllib.request, 'urlopen', side_effect=lambda *a, **k: Response(json.dumps(registry).encode())), \
             contextlib.redirect_stdout(io.StringIO()), self.assertRaisesRegex(ValueError, 'did not complete'):
            cleanup.verify_registries(data, ['https://global.test', 'https://cn.test'], attempts=1)


class ReleaseGateTests(unittest.TestCase):
    def classify(self, paths, truncated=False):
        text = (ROOT / '.github/workflows/validate-pr-comment.yml').read_text()
        source = text.split('          script: |\n', 1)[1].split('\n      - name: Post comment', 1)[0]
        source = textwrap.dedent(source)
        payload = {'source': source, 'paths': paths, 'truncated': truncated}
        runner = r'''
const fs = require('fs');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const outputs = {};
const context = {repo: {owner: 'owner', repo: 'repo'}, payload: {workflow_run: {
  head_sha: 'validated', head_branch: 'branch', head_repository: {owner: {login: 'dev'}}}}};
const core = {setOutput: (k, v) => outputs[k] = v, info: () => {}};
const github = {rest: {pulls: {
  list: async () => ({data: [{number: 42, base: {sha: 'immutable-base'}, head: {sha: 'validated', repo: {full_name: 'dev/repo'}}}]}),
  listFiles: () => {},
}, git: {getTree: async (args) => {
  if (args.tree_sha !== 'immutable-base' || args.recursive !== 'true') throw Error('wrong base tree');
  return {data: {truncated: input.truncated, tree: input.paths.map(path => ({type: 'blob', path}))}};
}}}, paginate: async () => [{filename: 'pool/main/sample/sample_2.0_arm64.deb.release.json'}]};
const AsyncFunction = Object.getPrototypeOf(async function() {}).constructor;
new AsyncFunction('github', 'context', 'core', input.source)(github, context, core)
  .then(() => process.stdout.write(JSON.stringify(outputs)))
  .catch(e => { process.stderr.write(e.message); process.exitCode = 1; });
'''
        return subprocess.run(['node', '-e', runner], input=json.dumps(payload), text=True, capture_output=True)

    def test_existing_manifest_is_an_update(self):
        result = self.classify(['pool/main/sample/sample_1.0_arm64.deb.release.json'])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['new_pkgs'], '')

    def test_metadata_only_directory_still_requires_first_release_review(self):
        result = self.classify(['pool/main/sample/meta.json', 'pool/main/sample/icon.png'])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['new_pkgs'], 'sample')

    def test_truncated_tree_cannot_auto_merge(self):
        result = self.classify([], truncated=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('truncated', result.stderr)


class OssSyncTests(unittest.TestCase):
    def sync(self, empty=False, fail=False):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        public = root / 'public'
        index = public / 'dists/stable/main/binary-arm64/Packages'
        index.parent.mkdir(parents=True)
        index.write_text('')
        (public / 'dists/stable/Release').write_text('Origin: Tests\n')
        (public / 'pool').mkdir()
        if not empty:
            pkg = public / 'pool/main/sample'
            pkg.mkdir(parents=True)
            (pkg / 'sample_2.0_arm64.deb').write_bytes(b'current')
        state = root / 'objects.json'
        state.write_text(json.dumps(['oss://bucket/os-list.json',
                                     'oss://bucket/packages/pool/main/sample/sample_1.0_arm64.deb',
                                     'oss://bucket/packages/pool/main/retired/meta.json']))
        executable = root / 'ossutil'
        executable.write_text('#!' + sys.executable + '\n' + textwrap.dedent('''
            import json, os, sys
            from pathlib import Path
            args = sys.argv[1:]
            with open(os.environ['FAKE_LOG'], 'a') as log:
                log.write(json.dumps(args) + '\\n')
            if os.environ['FAKE_FAIL'] == 'true':
                sys.exit(7)
            state = Path(os.environ['FAKE_STATE'])
            objects = json.loads(state.read_text())
            if args[0] == 'ls':
                destination = args[1]
                assert args[2:] == ['--short-format']
                assert destination.endswith('/pool/')
                listed = [o for o in objects if o.startswith(destination)]
                for obj in listed:
                    print(obj)
                print('Object Number is: ' + str(len(listed)))
            elif args[0] == 'rm':
                assert args[1] == '-f'
                objects.remove(args[2])
            elif args[0] == 'cp':
                objects.append(args[-1])
            state.write_text(json.dumps(objects))
        '''))
        executable.chmod(0o755)
        env = {**os.environ, 'PATH': str(root) + os.pathsep + os.environ['PATH'],
               'OSS_SYNC_ENABLED': 'true', 'OSS_BUCKET': 'bucket', 'OSS_PREFIX': 'packages',
               'OSS_PUBLIC_DIR': str(public), 'OSS_ENDPOINT': 'oss-cn-shenzhen.aliyuncs.com',
               'FAKE_LOG': str(root / 'commands.jsonl'), 'FAKE_STATE': str(state),
               'FAKE_FAIL': str(fail).lower(), 'OSS_SYNC_WORKERS': '1'}
        result = subprocess.run(['bash', str(ROOT / 'oss/sync-apt-repo.sh')], env=env, text=True, capture_output=True)
        self.assertTrue((root / 'commands.jsonl').exists(), result.stdout + result.stderr)
        calls = [json.loads(line) for line in (root / 'commands.jsonl').read_text().splitlines()]
        return result, json.loads(state.read_text()), calls

    def test_withdrawn_objects_removed_without_touching_bucket_files(self):
        result, objects, calls = self.sync()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('oss://bucket/os-list.json', objects)
        self.assertIn('oss://bucket/packages/pool/main/sample/sample_2.0_arm64.deb', objects)
        self.assertNotIn('oss://bucket/packages/pool/main/sample/sample_1.0_arm64.deb', objects)
        self.assertNotIn('oss://bucket/packages/pool/main/retired/meta.json', objects)
        self.assertIn('oss://bucket/packages/cn/registry.json', objects)

    def test_empty_catalog_clears_only_managed_pool(self):
        result, objects, calls = self.sync(empty=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('oss://bucket/os-list.json', objects)
        self.assertFalse(any('/pool/' in o for o in objects))

    def test_delete_failure_cannot_report_success_or_publish_catalog(self):
        result, objects, calls = self.sync(fail=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], 'ls')


if __name__ == '__main__':
    unittest.main()
