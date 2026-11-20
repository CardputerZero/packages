"""Delete withdrawn pool objects without re-uploading the remaining packages."""
from pathlib import Path
import re
import sys


def stale_objects(root, destination, listing):
    if not destination.startswith("oss://") or not destination.endswith("/pool/"):
        raise ValueError("Deletion destination must be the managed OSS pool/ prefix")
    objects = []
    counts = []
    for line in listing.splitlines():
        match = re.fullmatch(r"Object Number is: (\d+)", line)
        if line.startswith("oss://"):
            if not line.startswith(destination):
                raise ValueError("OSS listing contains an object outside the managed pool")
            objects.append(line)
        elif match:
            counts.append(int(match[1]))
        elif line and not re.fullmatch(r"\d+(?:\.\d+)?\(s\) elapsed", line):
            raise ValueError("Unrecognized OSS listing output; refusing pool cleanup")
    # ossutil 1.7.7 prints this footer once all pages have been listed. Validate the
    # complete result before any deletion, including names with embedded newlines.
    if counts != [len(objects)] or len(set(objects)) != len(objects):
        raise ValueError("Incomplete or ambiguous OSS listing; refusing pool cleanup")
    local = {destination + path.relative_to(root).as_posix()
             for path in root.rglob("*") if path.is_file() and not path.is_symlink()}
    return sorted(set(objects) - local)


def main():
    from oss_command import command

    root = Path(sys.argv[1])
    destination = sys.argv[2]
    if not root.is_dir():
        raise ValueError("Local pool directory does not exist")
    listing = command("ls", destination, "--short-format")
    for obj in stale_objects(root, destination, listing):
        print(f"Removing withdrawn object: {obj}", flush=True)
        command("rm", "-f", obj)


if __name__ == "__main__":
    main()
