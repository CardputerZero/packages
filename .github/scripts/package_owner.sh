#!/usr/bin/env bash
#
# Who owns a published package, by the manifests in the current checkout:
# package names are first-come-first-served, keyed on the uploader's GitHub
# login recorded as `uploaded_by` in the release manifest. Legacy manifests
# have no `uploaded_by`, so the login is derived from the published binary's
# Maintainer noreply address.
#
# Usage: package_owner.sh <package>
# Prints "<latest published version>|<owner login, lowercase>"; either part is
# empty when unknown (an unpublished package prints "|").

set -uo pipefail
PKG=$1

EXISTING="" ; LATEST_MANIFEST=""
for m in pool/main/"$PKG"/*.deb.release.json; do
  [ -f "$m" ] || continue
  v=$(jq -r '.version // empty' "$m")
  if [ -n "$v" ] && { [ -z "$EXISTING" ] || dpkg --compare-versions "$v" gt "$EXISTING"; }; then
    EXISTING="$v"; LATEST_MANIFEST="$m"
  fi
done

OWNER_LOGIN=""
if [ -n "$LATEST_MANIFEST" ]; then
  OWNER_LOGIN=$(jq -r '.uploaded_by // empty' "$LATEST_MANIFEST" | tr '[:upper:]' '[:lower:]')
  if [ -z "$OWNER_LOGIN" ]; then
    u=$(jq -r '.url // empty' "$LATEST_MANIFEST")
    if [ -n "$u" ] && curl -fsL --max-time 300 -o /tmp/existing.deb "$u" 2>/dev/null; then
      OM=$(dpkg-deb -f /tmp/existing.deb Maintainer 2>/dev/null | grep -oP '<\K[^>]+' | tr '[:upper:]' '[:lower:]' || true)
      OWNER_LOGIN=$(printf '%s' "$OM" | sed -nE 's/^([0-9]+\+)?([^@]+)@users\.noreply\.github\.com$/\2/p')
    fi
  fi
fi

printf '%s|%s\n' "$EXISTING" "$OWNER_LOGIN"
