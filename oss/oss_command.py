"""Bound every ossutil operation and retry only transient failures."""
import re
import subprocess
import time


TIMEOUTS = {"ls": 30, "stat": 30, "hash": 30, "cp": 120, "rm": 30}


def transient(message):
    status = re.search(r"StatusCode=(\d+)", message)
    if status:
        return int(status[1]) in (408, 429, 500, 502, 503, 504)
    return any(token in message.lower() for token in (
        "timeout", "timed out", "connection reset", "connection refused",
        "unexpected eof", "temporarily unavailable", "broken pipe",
    ))


def command(*args):
    timeout = TIMEOUTS[args[0]]
    label = " ".join(map(str, args))
    for attempt in range(1, 3):
        started = time.monotonic()
        try:
            result = subprocess.run(
                ["ossutil", *args], text=True, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            error = f"timed out after {timeout}s"
            retry = True
        else:
            elapsed = time.monotonic() - started
            if result.returncode == 0:
                if elapsed >= 5:
                    print(f"OSS {label} completed in {elapsed:.1f}s", flush=True)
                return result.stdout
            error = result.stdout.strip() or f"exit status {result.returncode}"
            retry = transient(error)
        elapsed = time.monotonic() - started
        if attempt == 2 or not retry or args[0] == "hash":
            raise RuntimeError(f"ossutil {label} failed after {elapsed:.1f}s: {error}")
        print(f"OSS {label} attempt {attempt} failed after {elapsed:.1f}s; retrying: {error}", flush=True)
        time.sleep(2)
