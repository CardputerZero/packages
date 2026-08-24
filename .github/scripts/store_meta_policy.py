#!/usr/bin/env python3
#
# What a package's store metadata (pool/main/<pkg>/meta.json) must contain,
# shared by both submission channels: process-web-submission.yml (the developer
# portal) and validate-pr.yml (`czdev publish` fork PRs). Also run by czdev
# locally as a pre-check, so a developer sees the same verdict before the PR
# exists. test/storemeta.test.js in dev-portal pins this file's behaviour.
#
# Usage: store_meta_policy.py <pkg> <pkg-dir> <pool-dir> <errors-out> \
#            [<warnings-out>] [<help-md-out>]
#
#   pkg          Debian package name (from the .deb control file)
#   pkg-dir      directory holding this package's meta.json + images
#   pool-dir     pool/main/ of the base branch, for uniqueness checks
#                (pass "-" to skip uniqueness, e.g. in local pre-checks
#                without a checkout)
#   errors-out   one blocking problem per line; nothing written when clean
#   warnings-out one advisory problem per line (missing description etc.)
#   help-md-out  markdown block stating the rules and a ready-to-use AI prompt
#
# Callers test with [ -s FILE ]. Exit code is 0 unless the script itself broke.

import io
import json
import re
import struct
import sys
from pathlib import Path

CATEGORIES = [
    "System Tools", "Development", "Hardware & IoT", "Security",
    "Radio & Comms", "AI", "Creative & Office", "Media", "Games",
    "Emulators", "Education", "Lifestyle", "Other",
]

PERMISSION_KEYS = [
    "camera", "microphone", "imu", "network",
    "additional_hardware", "background_service", "external_display",
]

SHARE_CODE_RE = re.compile(r"^[A-Za-z0-9]{4}$")
MAX_SCREENSHOTS = 6
SCREENSHOT_DIMS = (320, 170)
ICON_MIN, ICON_MAX = 128, 512

PNG_SIG = b"\x89PNG\r\n\x1a\n"


def png_dims(path):
    """(w, h) of a PNG, or None when the file is missing or not a PNG."""
    try:
        head = Path(path).open("rb").read(24)
    except OSError:
        return None
    if len(head) < 24 or head[:8] != PNG_SIG:
        return None
    return struct.unpack(">II", head[16:24])


def norm_title(s):
    return re.sub(r"\s+", " ", str(s)).strip().casefold()


def check_meta(pkg, pkg_dir, pool_dir):
    """(errors, warnings) for the package's store metadata."""
    errors, warnings = [], []
    pkg_dir = Path(pkg_dir)
    meta_path = pkg_dir / "meta.json"

    if not meta_path.is_file():
        return [f"缺少 meta.json（应位于 pool/main/{pkg}/meta.json）"], []
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except Exception as e:
        return [f"meta.json 不是合法的 JSON：{e}"], []
    if not isinstance(meta, dict):
        return ["meta.json 顶层必须是 JSON 对象"], []

    # --- required text fields ---
    title = meta.get("title")
    if not isinstance(title, str) or not title.strip():
        errors.append("title（应用显示名）必填，不能为空")
    summary = meta.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        errors.append("summary（一句话简介）必填，不能为空")
    elif len(summary.strip()) > 80:
        errors.append(f"summary 不能超过 80 字符（现在 {len(summary.strip())} 个）")

    license_ = meta.get("license")
    if not isinstance(license_, str) or not license_.strip():
        errors.append("license（许可证）必填，例如 MIT / GPL-3.0 / Apache-2.0")

    # --- categories: 1-2 out of the fixed enum ---
    cats = meta.get("categories")
    if not isinstance(cats, list) or not (1 <= len(cats) <= 2):
        errors.append("categories 必填：从固定分类枚举中选 1~2 个")
    else:
        for c in cats:
            if c not in CATEGORIES:
                errors.append(f"分类 {c!r} 不在枚举里；可选值：{', '.join(CATEGORIES)}")

    # --- author: a display name the store can render ---
    author = meta.get("author")
    if not isinstance(author, dict) or not str(author.get("display_name") or "").strip():
        errors.append("author.display_name（作者显示名）必填，商店列表和详情页都会展示它")

    # --- share_code ---
    code = meta.get("share_code")
    if not isinstance(code, str) or not SHARE_CODE_RE.match(code):
        errors.append("share_code 必填：4 位字母或数字（如 LOFI），用户可在商店里用它快速找到应用")

    # --- permissions: exactly the seven declared keys, all booleans ---
    perms = meta.get("permissions")
    if not isinstance(perms, dict):
        errors.append("permissions 必填：7 项布尔声明（"
                      + ", ".join(PERMISSION_KEYS) + "）")
    else:
        missing = [k for k in PERMISSION_KEYS if not isinstance(perms.get(k), bool)]
        extra = [k for k in perms if k not in PERMISSION_KEYS]
        if missing:
            errors.append("permissions 缺少布尔声明：" + ", ".join(missing))
        if extra:
            errors.append("permissions 含未定义的键：" + ", ".join(sorted(extra))
                          + "（只允许上述 7 项）")

    # --- icon: square PNG committed next to meta.json ---
    icon = meta.get("icon")
    if not isinstance(icon, str) or not icon.strip():
        errors.append("icon 必填：与 meta.json 同目录的 PNG 文件名")
    elif "/" in icon or icon.startswith("."):
        errors.append(f"icon 必须是同目录下的文件名，不能带路径：{icon!r}")
    else:
        d = png_dims(pkg_dir / icon)
        if d is None:
            errors.append(f"icon 文件缺失或不是 PNG：{icon}")
        elif d[0] != d[1] or not (ICON_MIN <= d[0] <= ICON_MAX):
            errors.append(f"icon 必须是正方形 PNG，边长 {ICON_MIN}~{ICON_MAX}"
                          f"（推荐 256×256），现在是 {d[0]}×{d[1]}")

    # --- screenshots: 1-6 PNGs at the device's screen size ---
    shots = meta.get("screenshots")
    if not isinstance(shots, list) or not shots:
        errors.append("screenshots 必填：至少 1 张、最多 "
                      f"{MAX_SCREENSHOTS} 张 {SCREENSHOT_DIMS[0]}×{SCREENSHOT_DIMS[1]} 的 PNG 截图")
    else:
        if len(shots) > MAX_SCREENSHOTS:
            errors.append(f"截图最多 {MAX_SCREENSHOTS} 张（现在 {len(shots)} 张）")
        for rel in shots:
            rel = str(rel)
            if not rel.startswith("screenshots/") or "/" in rel[len("screenshots/"):]:
                errors.append(f"截图路径必须形如 screenshots/<文件名>：{rel!r}")
                continue
            d = png_dims(pkg_dir / rel)
            if d is None:
                errors.append(f"截图文件缺失或不是 PNG：{rel}")
            elif d != SCREENSHOT_DIMS:
                errors.append(f"截图 {rel} 必须是 "
                              f"{SCREENSHOT_DIMS[0]}×{SCREENSHOT_DIMS[1]}，现在是 {d[0]}×{d[1]}")

    # --- uniqueness against every other package on the base branch ---
    if pool_dir and pool_dir != "-":
        mine_code = code.upper() if isinstance(code, str) else None
        mine_title = norm_title(title) if isinstance(title, str) else None
        for other in sorted(Path(pool_dir).glob("*/meta.json")):
            if other.parent.name == pkg:
                continue
            try:
                om = json.loads(other.read_text(encoding="utf-8"))
            except Exception:
                continue
            oc = om.get("share_code")
            if mine_code and isinstance(oc, str) and oc.upper() == mine_code:
                errors.append(f"share_code {code!r} 已被 {other.parent.name} 使用，分享码必须全局唯一")
            ot = om.get("title")
            if mine_title and isinstance(ot, str) and norm_title(ot) == mine_title:
                errors.append(f"应用显示名 {title!r} 已被 {other.parent.name} 使用，显示名必须全局唯一")

    # --- advisory: worth having, not blocking ---
    if not str(meta.get("description") or "").strip():
        warnings.append("建议提供 description（详细描述），商店详情页会完整展示")
    if not isinstance(meta.get("locales"), dict) or not meta.get("locales"):
        warnings.append("建议提供 locales（多语言 title/summary），商店会按用户语言展示")
    repo = meta.get("source_repo")
    if not str(repo or "").strip():
        warnings.append("建议提供 source_repo（开源仓库地址）")
    elif not str(repo).startswith(("http://", "https://")):
        errors.append(f"source_repo 必须是 http(s) 链接：{repo!r}")

    return errors, warnings


# One prompt for every surface this rule can reject from: czdev, the portal
# form, and both CI channels. Self-contained on purpose — pasting it into an
# assistant is enough to get app-builder.json (or the portal form) fixed.
PROMPT = """我在给 CardputerZero AppStore 发布一个应用，提交被商店的元数据检查拒绝了。
请帮我补全/修正元数据。如果我的项目用 czdev 发布，要改的是项目根目录 app-builder.json 的
"store" 段；如果我用 dev.cardputer.cc 网页上传，直接按下面的要求在表单里填写即可。

商店的元数据规则（meta.json 最终产物，czdev 会从 app-builder.json 的 store 段生成它）：

必填：
- title：应用显示名，全商店唯一（忽略大小写和多余空格）
- summary：一句话简介，不超过 80 字符
- categories：从固定枚举选 1~2 个：System Tools, Development, Hardware & IoT,
  Security, Radio & Comms, AI, Creative & Office, Media, Games, Emulators,
  Education, Lifestyle, Other
- screenshots：1~6 张 320×170 的 PNG 截图（CardputerZero 屏幕尺寸）
- icon：正方形 PNG，边长 128~512（推荐 256×256）
- author.display_name：作者显示名（商店列表和详情页展示）
- license：许可证，如 MIT / GPL-3.0 / Apache-2.0
- share_code：4 位字母或数字的分享码，全商店唯一
- permissions：7 项布尔声明：camera, microphone, imu, network,
  additional_hardware, background_service, external_display（不允许其他键）

建议（不阻断）：
- description：详细描述
- locales：多语言 title/summary（如 zh-CN / ja）
- source_repo：开源仓库地址（提供时必须是 http(s) 链接）

app-builder.json 的 store 段示例：

{{
  "app_name": "My App",
  "store": {{
    "summary": "One-line summary",
    "description": "Longer detail-page text",
    "categories": ["Games"],
    "screenshots": ["screenshots/main.png"],
    "icon": "packaging/icon.png",
    "license": "MIT",
    "source_repo": "https://github.com/you/my-app",
    "author": {{ "github": "you", "display_name": "Your Name" }},
    "share_code": "MYAP",
    "permissions": {{
      "camera": false, "microphone": false, "imu": false, "network": false,
      "additional_hardware": false, "background_service": false,
      "external_display": false
    }}
  }}
}}

我的包名是 {pkg}，这次被拒绝的原因：
{problems}

请告诉我具体要在哪个文件里加什么内容；如果截图/图标尺寸不对，请给出用
ImageMagick 或 ffmpeg 转换尺寸的命令。
另外：如果错误提到 icon/截图"不能带路径"，而我是用 czdev 发布的，
说明我的 AppBuilder 仓库太旧——先 git pull 更新它再重新 czdev publish 即可，
新版 czdev 会自动把路径转换成商店要求的形式。"""

HELP_TEMPLATE = """<details>
<summary>🏷️ 商店元数据规则 + 可直接交给 AI 的提示词（点开）</summary>

商店页面上的名字、简介、分类、截图、作者、许可证、分享码都来自这份元数据。
必填项缺失会导致商店里出现空作者、无分类、无截图的应用，所以提交时必须补全。

把下面整段复制给 AI（Cursor / Claude / ChatGPT），它带着完整规则和这次被拒的原因，
可以直接帮你改 app-builder.json 或指导网页表单怎么填：

```
{prompt}
```
</details>
"""


def main(argv):
    pkg, pkg_dir, pool_dir, err_out = argv[1], argv[2], argv[3], argv[4]
    warn_out = argv[5] if len(argv) > 5 else None
    help_out = argv[6] if len(argv) > 6 else None

    errors, warnings = check_meta(pkg, pkg_dir, pool_dir)

    if errors:
        Path(err_out).write_text("".join(f"{e}\n" for e in errors), encoding="utf-8")
    if warnings and warn_out:
        Path(warn_out).write_text("".join(f"{w}\n" for w in warnings), encoding="utf-8")
    if errors and help_out:
        problems = "\n".join(f"- {e}" for e in errors[:40])
        if len(errors) > 40:
            problems += f"\n-（另外 {len(errors) - 40} 条同类问题）"
        Path(help_out).write_text(
            HELP_TEMPLATE.format(prompt=PROMPT.format(pkg=pkg, problems=problems)),
            encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
