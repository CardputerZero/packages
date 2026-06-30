#!/usr/bin/env python3
"""Generate AppStore registry files from a built CardputerZero package tree."""

from __future__ import annotations

import argparse
import copy
import datetime as dt
import json
import urllib.parse
import uuid
from pathlib import Path
from typing import Any


NAMESPACE_CZ = uuid.UUID("c4d70000-a770-4000-8000-000000000000")
SUPPORTED_LOCALES = ["zh-CN", "en", "ja"]


def now_iso() -> str:
    return dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).strftime("%Y-%m-%dT%H:%M:%S+08:00")


def make_uuid(pkg_name: str, sha256_hex: str) -> str:
    return str(uuid.uuid5(NAMESPACE_CZ, f"{pkg_name}:{sha256_hex}"))


def parse_packages(path: Path) -> dict[str, dict[str, str]]:
    packages: dict[str, dict[str, str]] = {}
    current: dict[str, str] = {}
    current_key = ""

    def commit() -> None:
        if "Package" not in current:
            return
        pkg = current["Package"]
        if pkg not in packages or current.get("Version", "") > packages[pkg].get("Version", ""):
            packages[pkg] = dict(current)

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        if not raw_line:
            commit()
            current = {}
            current_key = ""
            continue
        if raw_line.startswith(" ") and current_key:
            current[current_key] = current[current_key] + "\n" + raw_line[1:]
            continue
        if ": " in raw_line:
            current_key, value = raw_line.split(": ", 1)
            current[current_key] = value
    commit()
    return packages


def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def list_value(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value if str(item)]
    if isinstance(value, str) and value:
        return [value]
    return []


def quote_path(path: str) -> str:
    return urllib.parse.quote(path.lstrip("/"), safe="/._+-")


def public_url(base_url: str, path: str) -> str:
    # Packages "Filename" may already be an absolute URL (e.g. when the .deb is
    # served straight from a GitHub Release). In that case use it verbatim
    # instead of prefixing the mirror base URL.
    if path.startswith(("http://", "https://")):
        return path
    return f"{base_url.rstrip('/')}/{quote_path(path)}"


def region_allowed(meta: dict[str, Any], region: str) -> bool:
    if not region:
        return True
    value = meta.get("region")
    if not isinstance(value, dict) or region not in value:
        return True
    region_value = value.get(region)
    return not (region_value is False or (isinstance(region_value, str) and region_value.lower() == "false"))


def asset_candidates(ref: str) -> list[str]:
    clean = ref.lstrip("/")
    candidates = [clean]
    if clean.startswith("share/images/"):
        candidates.append(clean.removeprefix("share/images/"))
    if clean.startswith("store/screenshots/"):
        candidates.append("screenshots/" + clean.removeprefix("store/screenshots/"))
    candidates.append(Path(clean).name)
    out: list[str] = []
    for item in candidates:
        if item and item not in out:
            out.append(item)
    return out


def resolve_asset_ref(pkg_dir: Path, pkg_name: str, base_url: str, ref: str) -> str:
    if not ref:
        return ""
    if ref.startswith(("http://", "https://")):
        return ref
    for candidate in asset_candidates(ref):
        if (pkg_dir / candidate).exists():
            return public_url(base_url, f"pool/main/{pkg_name}/{candidate}")
    return public_url(base_url, f"pool/main/{pkg_name}/{ref.lstrip('/')}")


def normalize_assets(pkg_dir: Path, pkg_name: str, base_url: str, meta: dict[str, Any]) -> dict[str, Any]:
    assets = meta.get("assets") if isinstance(meta.get("assets"), dict) else {}
    icon = str(assets.get("icon") or meta.get("icon") or "")
    screenshots = list_value(assets.get("screenshots") or meta.get("screenshots"))
    return {
        "icon": resolve_asset_ref(pkg_dir, pkg_name, base_url, icon),
        "screenshots": [resolve_asset_ref(pkg_dir, pkg_name, base_url, item) for item in screenshots],
    }


def build_app(pkg_name: str, pkg_info: dict[str, str], pkg_dir: Path,
              base_url: str, meta: dict[str, Any], generated_at: str) -> dict[str, Any]:
    sha256 = pkg_info.get("SHA256", "")
    filename = pkg_info.get("Filename", "")
    assets = normalize_assets(pkg_dir, pkg_name, base_url, meta)
    locales = meta.get("locales") if isinstance(meta.get("locales"), dict) else {}
    i18n = meta.get("i18n") if isinstance(meta.get("i18n"), dict) else {}
    if not locales and i18n:
        locales = i18n
    if not i18n and locales:
        i18n = locales
    published_at = str(meta.get("published_at") or meta.get("created_at") or generated_at)
    updated_at = str(meta.get("updated_at") or meta.get("published_at") or meta.get("created_at") or generated_at)
    review = meta.get("review") if isinstance(meta.get("review"), dict) else {}
    review_status = str(meta.get("review_status") or review.get("status") or "approved")
    review = {**review, "status": review_status}

    return {
        "uuid": str(meta.get("uuid") or make_uuid(pkg_name, sha256)),
        "share_code": str(meta.get("share_code") or pkg_name[:4]),
        "title": str(meta.get("title") or pkg_name),
        "summary": str(meta.get("summary") or ""),
        "description": str(meta.get("description") or pkg_info.get("Description") or ""),
        "locales": locales,
        "i18n": i18n,
        "categories": list_value(meta.get("categories")),
        "author": meta.get("author") if isinstance(meta.get("author"), dict) else {},
        "version": pkg_info.get("Version", ""),
        "published_at": published_at,
        "updated_at": updated_at,
        "review": review,
        "license": str(meta.get("license") or ""),
        "source_repo": str(meta.get("source_repo") or ""),
        "download": {
            "type": "deb",
            "package": pkg_name,
            "url": public_url(base_url, filename),
            "md5": pkg_info.get("MD5sum", ""),
            "sha256": sha256,
            "size": int(pkg_info.get("Size", 0) or 0),
        },
        "depends": pkg_info.get("Depends", ""),
        "permissions": meta.get("permissions") if isinstance(meta.get("permissions"), dict) else {},
        "assets": assets,
        "icon": assets["icon"],
        "screenshots": assets["screenshots"],
    }


def build_registry(packages_path: Path, pool_root: Path, base_url: str,
                   overrides_path: Path, region: str, registry_id: str) -> dict[str, Any]:
    packages = parse_packages(packages_path)
    overrides = read_json(overrides_path, {})
    generated_at = now_iso()
    apps = []
    for pkg_name, pkg_info in sorted(packages.items()):
        pkg_dir = pool_root / pkg_name
        meta_path = pkg_dir / "meta.json"
        if not meta_path.exists():
            continue
        meta = read_json(meta_path, {})
        if not isinstance(meta, dict) or not region_allowed(meta, region):
            continue
        override = overrides.get(pkg_name, {}) if isinstance(overrides, dict) else {}
        if isinstance(override, dict):
            meta = deep_merge(meta, override)
        apps.append(build_app(pkg_name, pkg_info, pkg_dir, base_url, meta, generated_at))

    return {
        "schema_version": 2,
        "generated_at": generated_at,
        "registry_id": registry_id,
        "region": region or "default",
        "device_targets": ["CardputerZero"],
        "i18n": {
            "default_locale": "en",
            "fallback_locale": "en",
            "supported_locales": SUPPORTED_LOCALES,
        },
        "apps": apps,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--packages", type=Path, required=True)
    parser.add_argument("--pool-root", type=Path, required=True)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--overrides", type=Path, default=Path("registry-overrides.json"))
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--region", default="")
    parser.add_argument("--registry-id", default="cardputerzero-appstore")
    args = parser.parse_args()

    registry = build_registry(
        args.packages,
        args.pool_root,
        args.base_url,
        args.overrides,
        args.region,
        args.registry_id,
    )
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "registry.json").write_text(
        json.dumps(registry, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(f"Generated registry with {len(registry['apps'])} app(s) at {args.out / 'registry.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
