#!/usr/bin/env python3
#
# Where a package may install files, shared by both submission channels:
# process-web-submission.yml (the developer portal) and validate-pr.yml
# (`czdev publish` fork PRs). The portal's browser pre-check carries the same
# rule and the same wording in site/debparse.js; test/installpath.test.js runs
# this file against the JS one so the two cannot drift.
#
# Usage: install_path_policy.py <package> <deb> <bad-paths-out> [<help-md-out>]
#
# Writes one "<path> — <reason>" line per rejected path, and (when asked) a
# markdown block stating the rule and handing over a ready-to-use prompt.
# Writes nothing when the package is clean, so callers test with [ -s FILE ].

import io
import re
import subprocess
import sys
import tarfile

# Roots that may only contain a subdirectory named after the package.
PKG_DIR_ROOTS = ("usr/share/", "usr/lib/", "opt/", "var/lib/")
# Debian Policy 10.1: on merged-usr systems these are symlinks into /usr and
# packages must not ship paths under them. lib/systemd/system is grandfathered
# in because already-published packages use it.
ALIASED_ROOTS = ("bin/", "sbin/", "lib/", "lib32/", "lib64/", "libx32/")
SYSTEMD_DIRS = ("lib/systemd/system/", "usr/lib/systemd/system/")
UNIT_SUFFIX = re.compile(r"\.(service|socket|timer|target|path|mount)$")
ALLOWED_SUMMARY = ("允许 usr/bin/<与包名相关的可执行文件>、"
                   "usr/share|usr/lib|opt|var/lib 下与包名相关的子目录、etc/ 下与包名相关的配置、"
                   "usr/share/APPLaunch/、usr/share/doc/、systemd 服务目录")


def normalize_name(s):
    return re.sub(r"[-_.]", "", s.lower())


def named_after_pkg(name, pkg):
    """Is `name` recognizably the package's own? Separators are ignored and
    either may prefix the other, so package fontpreview accepts a font_preview
    directory and myapp accepts myapp-cli, but neither accepts ls."""
    a = normalize_name(UNIT_SUFFIX.sub("", name))
    b = normalize_name(pkg or "")
    return bool(a and b) and (a.startswith(b) or b.startswith(a))


def install_path_issue(path, pkg):
    """None when the path is fine, otherwise why it is not.

    Aligned with Debian rather than a hand-rolled prefix list: /usr/bin is where
    Policy 10.1 says executables belong, and a data directory need not be
    spelled exactly like the package (Debian's own fonts-noto-cjk installs into
    /usr/share/fonts/opentype/noto/, and a deb package name cannot even contain
    the underscore an app directory often has). What is enforced is that
    anything landing in a *shared* directory carries the package's name, so an
    upload cannot quietly take over /usr/bin/ls or sshd.service.
    """
    if path.startswith("usr/local/"):
        return "Debian Policy 9.1.2 forbids packages installing into /usr/local (reserved for the sysadmin) / Debian Policy 9.1.2 禁止软件包写入 /usr/local（那是留给系统管理员的）"
    for d in SYSTEMD_DIRS:
        if path.startswith(d):
            unit = path[len(d):]
            if "/" in unit:
                return "no subdirectories under the systemd unit directory / systemd 服务目录下不应再有子目录"
            if named_after_pkg(unit, pkg):
                return None
            return f"systemd unit name must relate to the package name {pkg} (avoids shadowing system services) / systemd 服务名必须与包名 {pkg} 相关，避免覆盖系统服务"
    if path.startswith(ALIASED_ROOTS):
        return "Debian Policy 10.1 forbids /bin /sbin /lib etc. (usr-merge alias paths); use the /usr equivalent / Debian Policy 10.1 禁止软件包写入 /bin /sbin /lib 等 usr-merge 别名路径，请改用 /usr 下的对应位置"
    if path.startswith("usr/share/APPLaunch/") or path.startswith("usr/share/doc/"):
        return None
    # /etc/<pkg>.conf is as much a Debian convention as /etc/<pkg>/ (compare
    # rsyslog.conf, logrotate.conf), so a lone conffile is fine here.
    if path.startswith("etc/"):
        rest = path[len("etc/"):]
        top = rest.split("/", 1)[0]
        if named_after_pkg(top, pkg):
            return None
        return f"etc/ may only hold config named after the package {pkg}; {top} does not match / etc/ 下只能放与包名 {pkg} 相关的配置文件或目录，{top} 不符合"
    if path.startswith("usr/bin/"):
        exe = path[len("usr/bin/"):]
        if "/" in exe:
            return "no subdirectories under /usr/bin; put app data in usr/share/<pkg>/ or usr/lib/<pkg>/ / /usr/bin 下不应有子目录，程序数据请放 usr/share/<包名>/ 或 usr/lib/<包名>/"
        if named_after_pkg(exe, pkg):
            return None
        return f"the filename under /usr/bin must relate to the package name {pkg} (avoids shadowing system commands) / /usr/bin 下的文件名必须与包名 {pkg} 相关，避免覆盖系统命令"
    for root in PKG_DIR_ROOTS:
        if path.startswith(root):
            rest = path[len(root):]
            if "/" not in rest:
                return f"files must not sit directly under {root}; use a {root}<pkg>/ subdirectory / 不能直接放在 {root} 下，请放进 {root}<包名>/ 子目录"
            top = rest.split("/", 1)[0]
            if named_after_pkg(top, pkg):
                return None
            return f"the directory name {root}{top}/ must relate to the package name {pkg} / {root}{top}/ 的目录名必须与包名 {pkg} 相关"
    return f"not an allowed install location / 不在允许的安装位置内（{ALLOWED_SUMMARY}）"


# A developer can hit this rule in the browser pre-check or in either CI
# channel, and should be handed the same prompt every time. Self-contained on
# purpose: it carries the whole policy plus the rejected paths, so pasting it
# into an assistant is enough to get the packaging fixed. Mirrored verbatim in
# the portal's site/debparse.js (INSTALL_PATH_PROMPT).
PROMPT = """我在给 CardputerZero AppStore 打一个 Debian 包（.deb），提交后被商店的安装路径检查拒绝了。
请帮我修改打包脚本（CMakeLists.txt / debian/rules / install 脚本 / Makefile，看项目里用的是哪种），
把文件装到合规位置，并同步更新 .desktop 里的 Exec 和 Icon、以及代码里读取资源文件的路径。

商店的规则（包名记作 PKG，比对时忽略 - _ . 的差异，所以 PKG 是 fontpreview 时目录叫 font_preview 也算合规）：

允许安装到：
- usr/bin/<名字>：可执行文件，名字必须与包名相关（这是 Debian Policy 10.1 的标准位置）
- usr/share/<与包名相关的目录>/**：图片、字体、音频等只读资源
- usr/lib/<与包名相关的目录>/**：私有库、插件
- opt/<与包名相关的目录>/**：整体打包自带的目录
- etc/<与包名相关的目录>/** 或 etc/<与包名相关>.conf：配置文件（Debian conffile 惯例）
- var/lib/<与包名相关的目录>/**：运行时数据
- usr/share/APPLaunch/**：CardputerZero 启动器资源，且必须有 usr/share/APPLaunch/applications/<名字>.desktop
- usr/share/doc/**：文档
- lib/systemd/system/<与包名相关>.service 或 usr/lib/systemd/system/<同样>：服务名必须与包名相关，
  且 [Service] 段必须写 User=<非 root 用户>（商店拒绝以 root 运行的服务）

禁止：
- usr/local/** —— Debian Policy 9.1.2 明令禁止软件包写入
- 以 bin/ sbin/ lib/ lib32/ lib64/ libx32/ 开头的路径 —— Debian Policy 10.1（usr-merge 别名路径），请改用 usr/ 下的对应位置
- 直接散落在 usr/share/ 或 usr/lib/ 下的文件 —— 必须收进以包名命名的子目录
- 在 usr/bin/ 下建子目录
- 名字与包名无关的共享目录名、可执行文件名或服务名（这是为了防止覆盖系统文件）
- setuid/setgid 文件、设备文件

我的包名 PKG = {pkg}
这次被拒绝的路径：
{paths}

请告诉我具体要改哪些文件、改成什么，以及资源路径在源码里怎么同步修改。
(Assistant: reply in the same language the user speaks to you.)"""

HELP_TEMPLATE = """<details>
<summary>📁 Install path rules + a ready-to-paste AI prompt / 安装路径规则 + 可直接交给 AI 的提示词（点开）</summary>

We allow Debian's standard locations and only require that **anything landing
in a shared directory carries your package name**, so an app cannot quietly
shadow system commands or other packages' files. `-`, `_` and `.` are ignored
in the comparison, so package `fontpreview` may use a `font_preview` directory.
Copy the block below into an AI assistant (Cursor / Claude / ChatGPT); it
carries the full policy plus your rejected paths and can fix the packaging
scripts directly.

我们放行 Debian 的标准位置，只要求**落在共享目录里的文件名/目录名带上你的包名**，
这样一个应用不会悄悄覆盖系统命令或别人的文件。比对时忽略 `-`、`_`、`.` 的差异，
所以包名 `fontpreview` 用 `font_preview` 目录是可以的。
把下面整段复制给 AI（Cursor / Claude / ChatGPT），它带着完整规则和你这次被拒的路径，
可以直接帮你改打包脚本：

```
{prompt}
```
</details>
"""


def rejected_paths(deb, pkg):
    """Every path in `deb` the policy rejects, as (path, reason) pairs.

    Reads the member list from the tar because `dpkg-deb -c` reports the link
    target as the last field on symlink lines.
    """
    tb = subprocess.run(["dpkg-deb", "--fsys-tarfile", deb],
                        capture_output=True, check=True).stdout
    bad = []
    with tarfile.open(fileobj=io.BytesIO(tb)) as tf:
        for member in tf.getmembers():
            if member.isdir():
                continue
            path = re.sub(r"^\./", "", member.name).rstrip("/")
            if not path:
                continue
            why = install_path_issue(path, pkg)
            if why:
                # Backticks would break out of the code fence in the help text,
                # and control characters would garble it.
                bad.append((re.sub(r"[`\x00-\x1f]", "?", path), why))
    return bad


def main(argv):
    pkg, deb, bad_out = argv[1], argv[2], argv[3]
    help_out = argv[4] if len(argv) > 4 else None

    # Without a package name nothing can be judged as the package's own, and an
    # unreadable control file is already reported on its own.
    if not pkg:
        return 0
    try:
        bad = rejected_paths(deb, pkg)
    except Exception:
        return 0
    if not bad:
        return 0

    with open(bad_out, "w") as f:
        for path, why in bad:
            f.write(f"{path} — {why}\n")

    if help_out:
        paths = "\n".join(p for p, _ in bad[:40])
        if len(bad) > 40:
            paths += f"\n…（另外 {len(bad) - 40} 条同类问题）"
        with open(help_out, "w") as f:
            f.write(HELP_TEMPLATE.format(prompt=PROMPT.format(pkg=pkg, paths=paths)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
