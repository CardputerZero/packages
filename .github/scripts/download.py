#!/usr/bin/env python3
"""Download release uploads with bounded retries and atomic, verified output."""
import argparse
import hashlib
import http.client
import os
from pathlib import Path
import tempfile
import time
import urllib.error
import urllib.request


RETRY_STATUS = {404, 408, 409, 425, 429, 500, 502, 503, 504}
DELAYS = (15, 30, 60, 120, 180)


def download(url, output, sha256=None, *, budget=600, delays=DELAYS):
    """Never expose a partial file, or retry a checksum/policy failure."""
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + budget
    for attempt in range(len(delays) + 1):
        temporary = None
        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("download retry budget exhausted")
            req = urllib.request.Request(url, headers={"User-Agent": "cz-store", "Accept-Encoding": "identity"})
            digest = hashlib.sha256()
            with urllib.request.urlopen(req, timeout=min(120, remaining)) as response:
                expected = response.headers.get("Content-Length")
                received = 0
                with tempfile.NamedTemporaryFile(dir=output.parent, delete=False) as dest:
                    temporary = Path(dest.name)
                    while True:
                        if time.monotonic() >= deadline:
                            raise TimeoutError("download retry budget exhausted")
                        chunk = response.read(1024 * 1024)
                        if not chunk:
                            break
                        dest.write(chunk)
                        digest.update(chunk)
                        received += len(chunk)
                if expected is not None and received != int(expected):
                    raise http.client.IncompleteRead(b"", int(expected) - received)
            if sha256 and digest.hexdigest().lower() != sha256.lower():
                raise ValueError("checksum mismatch for uploaded file")
            os.replace(temporary, output)
            return output
        except (urllib.error.URLError, TimeoutError, ConnectionError, http.client.HTTPException) as exc:
            if isinstance(exc, urllib.error.HTTPError) and exc.code not in RETRY_STATUS:
                raise
            if attempt == len(delays) or time.monotonic() + delays[attempt] >= deadline:
                raise
            delay = delays[attempt]
            if isinstance(exc, urllib.error.HTTPError):
                retry_after = exc.headers.get("Retry-After", "") if exc.headers else ""
                if retry_after.isdigit():
                    delay = max(delay, int(retry_after))
            if time.monotonic() + delay >= deadline:
                raise
            print(f"Download temporarily unavailable ({type(exc).__name__}); retrying in {delay}s", flush=True)
            time.sleep(delay)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url")
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    try:
        download(args.url, args.output)
    except (OSError, ValueError, http.client.HTTPException) as exc:
        parser.exit(1, f"download temporarily unavailable: {exc}\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url")
    parser.add_argument("output", type=Path)
    parser.add_argument("--sha256")
    args = parser.parse_args()
    try:
        download(args.url, args.output, args.sha256)
    except ValueError as exc:
        print(str(exc))
        return 2
    except (urllib.error.URLError, TimeoutError, ConnectionError, http.client.HTTPException) as exc:
        print(f"Download temporarily unavailable: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
