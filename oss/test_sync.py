import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import unittest
from urllib.parse import unquote, urlsplit


SCRIPT = Path(__file__).with_name("sync-apt-repo.sh")


def crc64(data):
    value = 0xFFFFFFFFFFFFFFFF
    for byte in data:
        value ^= byte
        for _ in range(8):
            value = (value >> 1) ^ (0xC96C5795D7870F42 if value & 1 else 0)
    return value ^ 0xFFFFFFFFFFFFFFFF


class ObjectHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def object_key(self):
        return unquote(urlsplit(self.path).path).removeprefix("/test-bucket/")

    def error(self, status, code):
        body = f"<Error><Code>{code}</Code><Message>Test failure</Message></Error>".encode()
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("x-oss-request-id", "test-request")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_HEAD(self):
        key = self.object_key()
        if self.server.stat_status:
            self.error(self.server.stat_status, "AccessDenied")
            return
        if key not in self.server.objects:
            self.error(404, "NoSuchKey")
            return
        body = self.server.objects[key]
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("ETag", '"' + hashlib.md5(body).hexdigest().upper() + '"')
        if key not in self.server.without_crc:
            self.send_header("x-oss-hash-crc64ecma", str(crc64(body)))
        self.send_header("x-oss-object-type", "Normal")
        self.end_headers()

    def do_GET(self):
        key = self.object_key()
        if self.server.stat_status:
            self.error(self.server.stat_status, "AccessDenied")
        elif key not in self.server.objects:
            self.error(404, "NoSuchKey")
        elif urlsplit(self.path).query == "acl":
            body = (b"<AccessControlPolicy><Owner><ID>test-owner</ID></Owner>"
                    b"<AccessControlList><Grant>private</Grant></AccessControlList>"
                    b"</AccessControlPolicy>")
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.error(400, "InvalidArgument")

    def do_PUT(self):
        key = self.object_key()
        if self.headers.get("Transfer-Encoding") == "chunked":
            chunks = []
            while size := int(self.rfile.readline().split(b";")[0], 16):
                chunks.append(self.rfile.read(size))
                self.rfile.read(2)
            self.rfile.readline()
            body = b"".join(chunks)
        else:
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        if self.server.fail_after == len(self.server.uploads):
            self.error(403, "AccessDenied")
            return
        self.server.objects[key] = body
        self.server.uploads.append(key)
        self.server.without_crc.discard(key)
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.send_header("ETag", '"' + hashlib.md5(body).hexdigest().upper() + '"')
        self.send_header("x-oss-hash-crc64ecma", str(crc64(body)))
        self.end_headers()


class SyncTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        binary = os.environ.get("OSSUTIL_TEST_BINARY") or shutil.which("ossutil")
        if not binary:
            raise RuntimeError("Set OSSUTIL_TEST_BINARY to an ossutil 1.7.7 executable")
        cls.binary = str(Path(binary).resolve())

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "public"
        self.write("dists/stable/Release", b"Release fixture\n")
        self.write("dists/stable/main/binary-arm64/Packages", b"")
        self.write("pool/main/example/example_1.0_arm64.deb", b"package-v1")
        self.write("pool/main/example/meta.json", b'{"title": "Example"}\n')
        self.write("pool/main/example/screenshots/with space.png", b"image-v1")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), ObjectHandler)
        self.server.objects = {}
        self.server.uploads = []
        self.server.without_crc = set()
        self.server.stat_status = None
        self.server.fail_after = None
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

        config = self.root / "ossutil.config"
        config.write_text(
            "[Credentials]\n"
            "language=EN\n"
            f"endpoint=http://127.0.0.1:{self.server.server_port}\n"
            "accessKeyID=test-key\n"
            "accessKeySecret=test-secret\n"
        )
        wrapper = self.root / "bin/ossutil"
        wrapper.parent.mkdir()
        wrapper.write_text(
            '#!/bin/sh\n'
            'if [ "$1" = hash ]; then\n'
            '  exec "$OSSUTIL_TEST_BINARY" "$@"\n'
            'fi\n'
            'exec "$OSSUTIL_TEST_BINARY" -c "$OSSUTIL_TEST_CONFIG" "$@"\n'
        )
        wrapper.chmod(0o755)
        self.env = {
            **os.environ,
            "PATH": f"{wrapper.parent}:{os.environ['PATH']}",
            "OSSUTIL_TEST_BINARY": self.binary,
            "OSSUTIL_TEST_CONFIG": str(config),
            "OSS_SYNC_ENABLED": "true",
            "OSS_PUBLIC_DIR": str(self.source),
            "OSS_BUCKET": "test-bucket",
            "OSS_PREFIX": "packages",
            "OSS_ENDPOINT": "oss-eu-central-1.aliyuncs.com",
        }

    def write(self, relative, content):
        path = self.source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def run_sync(self, success=True):
        result = subprocess.run(
            ["bash", str(SCRIPT)], env=self.env, cwd=self.root,
            text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=60,
        )
        if success:
            self.assertEqual(result.returncode, 0, result.stdout)
        else:
            self.assertNotEqual(result.returncode, 0, result.stdout)
        return result

    def test_unchanged_content_is_skipped_after_mtime_changes(self):
        self.assertEqual(crc64(b"123456789"), 0x995DC9BBDF1939FA)
        self.run_sync()
        self.assertEqual(len(self.server.uploads), 5)
        self.server.uploads.clear()
        for path in self.source.rglob("*"):
            if path.is_file():
                os.utime(path, (2000000000, 2000000000))
        self.run_sync()
        self.assertEqual(self.server.uploads, [])

    def test_same_size_content_change_is_uploaded(self):
        self.run_sync()
        self.server.uploads.clear()
        self.write("pool/main/example/example_1.0_arm64.deb", b"package-v2")
        self.run_sync()
        key = "packages/pool/main/example/example_1.0_arm64.deb"
        self.assertEqual(self.server.uploads, [key])
        self.assertEqual(self.server.objects[key], b"package-v2")

    def test_new_file_and_missing_remote_object_are_uploaded(self):
        self.run_sync()
        self.server.uploads.clear()
        missing = "packages/pool/main/example/meta.json"
        del self.server.objects[missing]
        self.write("pool/main/example/new.png", b"new image")
        self.run_sync()
        self.assertCountEqual(self.server.uploads, [missing, "packages/pool/main/example/new.png"])

    def test_remote_stat_failure_stops_sync(self):
        self.server.stat_status = 403
        self.run_sync(success=False)
        self.assertEqual(self.server.uploads, [])

    def test_retry_skips_files_uploaded_before_failure(self):
        self.server.fail_after = 1
        self.run_sync(success=False)
        completed = set(self.server.objects)
        self.assertEqual(len(completed), 1)
        self.server.fail_after = None
        self.server.uploads.clear()
        self.run_sync()
        self.assertEqual(len(self.server.objects), 5)
        self.assertEqual(len(self.server.uploads), 4)
        self.assertTrue(completed.isdisjoint(self.server.uploads))

    def test_object_without_crc_is_uploaded_once(self):
        self.run_sync()
        self.server.uploads.clear()
        key = "packages/pool/main/example/example_1.0_arm64.deb"
        self.server.without_crc.add(key)
        self.run_sync()
        self.assertEqual(self.server.uploads, [key])
        self.server.uploads.clear()
        self.run_sync()
        self.assertEqual(self.server.uploads, [])

    def test_empty_prefix_uses_default_and_empty_file_is_skipped(self):
        self.env["OSS_PREFIX"] = ""
        self.run_sync()
        key = "packages/dists/stable/main/binary-arm64/Packages"
        self.assertIn(key, self.server.objects)
        self.assertEqual(self.server.objects[key], b"")
        self.server.uploads.clear()
        self.run_sync()
        self.assertEqual(self.server.uploads, [])

    def test_disabled_sync_uploads_nothing(self):
        self.env["OSS_SYNC_ENABLED"] = "false"
        self.run_sync()
        self.assertEqual(self.server.objects, {})

    def test_cn_filter_preserves_source_and_excludes_blocked_package(self):
        self.env["OSS_ENDPOINT"] = "https://oss-cn-shenzhen.aliyuncs.com"
        for package, allowed in (("example", True), ("blocked", False)):
            staging = self.root / f"deb-{package}"
            (staging / "DEBIAN").mkdir(parents=True)
            (staging / "DEBIAN/control").write_text(
                f"Package: {package}\nVersion: 1.0\nArchitecture: arm64\n"
                "Maintainer: Test <test@example.org>\nDescription: Test package\n"
            )
            destination = self.source / f"pool/main/{package}/{package}_1.0_arm64.deb"
            destination.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(["dpkg-deb", "--build", str(staging), str(destination)],
                           check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            self.write(f"pool/main/{package}/meta.json",
                       json.dumps({"title": package, "region": {"CN": allowed}}).encode())
        self.run_sync()
        self.assertTrue((self.source / "pool/main/blocked/meta.json").is_file())
        self.assertFalse(any("/blocked/" in key for key in self.server.objects))
        packages = self.server.objects["packages/dists/stable/main/binary-arm64/Packages"]
        self.assertIn(b"Package: example", packages)
        self.assertNotIn(b"Package: blocked", packages)
        registry = json.loads(self.server.objects["packages/cn/registry.json"])
        self.assertEqual([app["download"]["package"] for app in registry["apps"]], ["example"])


if __name__ == "__main__":
    unittest.main()
