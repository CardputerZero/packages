#!/usr/bin/env bash
set -euo pipefail

enabled="${OSS_SYNC_ENABLED:-false}"
enabled="$(printf '%s' "$enabled" | tr '[:upper:]' '[:lower:]')"

case "$enabled" in
  1|true|yes|on)
    ;;
  *)
    echo "OSS sync disabled (OSS_SYNC_ENABLED=${OSS_SYNC_ENABLED:-false})"
    exit 0
    ;;
esac

public_dir="${OSS_PUBLIC_DIR:-public}"
public_dir="${public_dir%/}"
bucket="${OSS_BUCKET:?OSS_BUCKET is required when OSS sync is enabled}"
prefix="${OSS_PREFIX:-packages}"
prefix="${prefix#/}"
prefix="${prefix%/}"
endpoint="${OSS_ENDPOINT:-oss-cn-shenzhen.aliyuncs.com}"
endpoint_host="${endpoint#http://}"
endpoint_host="${endpoint_host#https://}"
endpoint_host="${endpoint_host%%/*}"
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if ! command -v ossutil >/dev/null 2>&1; then
  echo "ossutil is required when OSS sync is enabled" >&2
  exit 1
fi

if [[ ! -d "$public_dir" ]]; then
  echo "public directory not found: $public_dir" >&2
  exit 1
fi

if [[ ! -f "$public_dir/dists/stable/Release" ]]; then
  echo "APT Release file not found under $public_dir/dists/stable" >&2
  exit 1
fi

if [[ ! -f "$public_dir/dists/stable/main/binary-arm64/Packages" ]]; then
  echo "APT Packages file not found under $public_dir/dists/stable/main/binary-arm64" >&2
  exit 1
fi

sync_dir="$public_dir"
tmp_dir=""
cleanup() {
  if [[ -n "$tmp_dir" ]]; then
    rm -rf "$tmp_dir"
  fi
}
trap cleanup EXIT

write_release() {
  local stable_dir="$1/dists/stable"
  local release_file="$stable_dir/Release"
  printf '%s\n' \
    "Origin: CardputerZero" \
    "Label: CardputerZero AppStore" \
    "Suite: stable" \
    "Codename: stable" \
    "Architectures: arm64" \
    "Components: main" \
    "Description: CardputerZero APT Repository" > "$release_file"
  echo "MD5Sum:" >> "$release_file"
  for f in main/binary-arm64/Packages main/binary-arm64/Packages.gz; do
    size=$(wc -c < "$stable_dir/$f" | tr -d ' ')
    hash=$(md5sum "$stable_dir/$f" | awk '{print $1}')
    printf " %s %s %s\n" "$hash" "$size" "$f" >> "$release_file"
  done
  echo "SHA256:" >> "$release_file"
  for f in main/binary-arm64/Packages main/binary-arm64/Packages.gz; do
    size=$(wc -c < "$stable_dir/$f" | tr -d ' ')
    hash=$(sha256sum "$stable_dir/$f" | awk '{print $1}')
    printf " %s %s %s\n" "$hash" "$size" "$f" >> "$release_file"
  done
}

if [[ "$endpoint_host" == "oss-cn-shenzhen.aliyuncs.com" ]]; then
  tmp_dir="$(mktemp -d)"
  sync_dir="$tmp_dir/public"
  mkdir -p "$sync_dir"
  cp -a "$public_dir/." "$sync_dir/"

  blocked_file="$tmp_dir/blocked-packages.txt"
  python3 - "$sync_dir" > "$blocked_file" <<'PY'
import json
import sys
from pathlib import Path

root = Path(sys.argv[1]) / "pool" / "main"
if not root.exists():
    sys.exit(0)

for meta_path in sorted(root.glob("*/meta.json")):
    try:
        data = json.loads(meta_path.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"Invalid meta.json: {meta_path}: {exc}", file=sys.stderr)
        sys.exit(1)

    region = data.get("region")
    if not isinstance(region, dict) or "CN" not in region:
        continue

    cn = region.get("CN")
    if cn is False or (isinstance(cn, str) and cn.lower() == "false"):
        print(meta_path.parent.name)
PY

  blocked_count=0
  while IFS= read -r pkg; do
    [[ -n "$pkg" ]] || continue
    echo "Skipping ${pkg}: meta.json region.CN=false for ${endpoint_host}"
    rm -rf "$sync_dir/pool/main/$pkg"
    blocked_count=$((blocked_count + 1))
  done < "$blocked_file"

  if ((blocked_count > 0)); then
    apt_dir="$sync_dir/dists/stable/main/binary-arm64"
    mkdir -p "$apt_dir"
    (cd "$sync_dir" && dpkg-scanpackages --multiversion pool/ > dists/stable/main/binary-arm64/Packages)
    gzip -k -f "$apt_dir/Packages"
    write_release "$sync_dir"
  fi

  public_base_url="${OSS_PUBLIC_BASE_URL:-https://${bucket}.${endpoint_host}}"
  public_base_url="${public_base_url%/}"
  registry_base_url="$public_base_url"
  if [[ -n "$prefix" ]]; then
    registry_base_url="${registry_base_url}/${prefix}"
  fi
  python3 "$script_dir/generate-registry.py" \
    --packages "$sync_dir/dists/stable/main/binary-arm64/Packages" \
    --pool-root "$sync_dir/pool/main" \
    --base-url "$registry_base_url" \
    --out "$sync_dir/cn" \
    --region CN \
    --registry-id cardputerzero-appstore-cn
  echo "Generated CN registry: ${registry_base_url}/cn/registry.json"
fi

dest_base="oss://${bucket}"
if [[ -n "$prefix" ]]; then
  dest_base="${dest_base}/${prefix}"
fi

count=0
# Only pool/ is an exact mirror. Never --delete at the bucket root: it can
# contain OS images and other repositories. Empty catalogs intentionally clear
# the managed pool too. A failed listing/delete must fail the deployment.
mkdir -p "$sync_dir/pool"
ossutil sync "$sync_dir/pool/" "${dest_base}/pool/" --delete -f
while IFS= read -r -d '' file; do
  rel="${file#"$sync_dir"/}"
  [[ "$rel" == pool/* ]] && continue
  echo "Uploading ${rel} -> ${dest_base}/${rel}"
  ossutil cp -f "$file" "${dest_base}/${rel}"
  count=$((count + 1))
done < <(find "$sync_dir" -type f -print0)

echo "OSS sync complete: ${count} files uploaded to ${dest_base}/"
