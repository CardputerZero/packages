#!/usr/bin/env python3
#
# The package facts a reviewer wants at a glance, as a markdown table, shared by
# both submission channels: process-web-submission.yml puts it in the publish
# PR body, validate-pr.yml in the result comment on a `czdev publish` PR. The
# portal's pre-check shows the same facts before upload.
#
# Usage: deb_summary.py <deb> [<published-version>]
#
# Prints the table on stdout. Never fails the caller: an unreadable field is
# shown as "?".

import io
import os
import subprocess
import sys
import tarfile

EM_AARCH64 = 183


def field(deb, name):
    try:
        return subprocess.run(["dpkg-deb", "-f", deb, name],
                              capture_output=True, text=True, check=True).stdout.strip()
    except Exception:
        return ""


def mib(n):
    return f"{n / 1048576:.1f} MB"


def contents(deb):
    """(file count, ELF count, set of ELF machine names) from the data tarball."""
    files = elf = 0
    machines = set()
    tb = subprocess.run(["dpkg-deb", "--fsys-tarfile", deb], capture_output=True, check=True).stdout
    with tarfile.open(fileobj=io.BytesIO(tb)) as tf:
        for m in tf.getmembers():
            if not m.isfile():
                continue
            files += 1
            head = tf.extractfile(m).read(20)
            if len(head) >= 20 and head[:4] == b"\x7fELF":
                elf += 1
                machine = int.from_bytes(head[18:20], "little")
                machines.add("arm64" if machine == EM_AARCH64 else f"e_machine={machine}")
    return files, elf, machines


def cell(s):
    # Field values come from the uploaded .deb: keep them from breaking the table.
    return str(s).replace("|", "\\|").replace("\n", " ").strip() or "?"


def main(argv):
    deb = argv[1]
    published = argv[2] if len(argv) > 2 else ""
    installed_kb = field(deb, "Installed-Size")
    rows = [
        ("Package", f"`{field(deb, 'Package')}`"),
        ("Version", field(deb, "Version")),
        ("Previous version / 上一版", published or "— (new package / 新应用)"),
        ("Architecture", field(deb, "Architecture")),
        ("Maintainer", field(deb, "Maintainer")),
        ("Download size / 安装包大小", mib(os.path.getsize(deb))),
        ("Installed size / 安装后大小", mib(int(installed_kb) * 1024) if installed_kb.isdigit() else "?"),
    ]
    try:
        files, elf, machines = contents(deb)
        rows.append(("Files / 文件数", files))
        rows.append(("ELF binaries / ELF 二进制",
                     f"{elf} ({', '.join(sorted(machines))})" if elf else "0"))
    except Exception:
        rows.append(("Files / 文件数", "?"))
    depends = field(deb, "Depends")
    if depends:
        rows.append(("Depends", depends))

    print("| Field | Value |")
    print("|---|---|")
    for k, v in rows:
        print(f"| {k} | {cell(v)} |")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
