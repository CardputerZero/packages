# CardputerZero Packages

APT repository for CardputerZero applications.

`.deb` binaries are **not** stored in git. They live in a long-lived GitHub
Release (`apt-pool`) and are mirrored to Aliyun OSS for China. Git only holds
small metadata: `meta.json`, screenshots, and one
`<pkg>_<ver>_<arch>.deb.release.json` manifest per package.

## How the device installs apps

The on-device AppStore reads `registry.json` and downloads each `.deb` directly
from its `download.url` (an absolute GitHub Release URL globally, or an OSS URL
for the China registry), verifying `download.md5` before install. This is the
primary install path and needs no apt configuration.

## Manual apt (optional, power users)

`apt` requires the `.deb` to be co-located with the metadata (it treats
`Filename` as a path relative to the repo root), so the installable apt endpoint
is the **OSS mirror**, which serves the binaries inline:

```bash
echo "deb [trusted=yes] https://cardputer-zero-repo.oss-cn-shenzhen.aliyuncs.com/packages stable main" | sudo tee /etc/apt/sources.list.d/cardputerzero.list
sudo apt update
sudo apt install nc2000
```

The GitHub Pages endpoint (`https://cardputerzero.github.io/packages`) publishes
the index with absolute Release `Filename`s — it is the **data source for the
registry** and the web UI, not a `apt`-installable mirror (apt does not follow
absolute `Filename` URLs).

## Architecture

```
Storage:   .deb  →  GitHub Release "apt-pool"  (unlimited bandwidth, no LFS)
                 →  Aliyun OSS mirror           (China, relative paths)
Metadata:  git   →  meta.json / screenshots / *.deb.release.json manifests
Index:     CI    →  Pages: dists/ + pool/(metadata only), Filename → release URL
                 →  OSS:   full tree with .deb, relative Filename + CN registry
```

- **Global / Pages**: `dists/.../Packages` `Filename` points at the `apt-pool`
  release asset URL. Pages serves only metadata, so it never approaches the
  1 GB site limit.
- **China / OSS**: CI downloads the `.deb` from the release, uploads them to
  OSS, and regenerates `Packages` with **relative** `Filename` so apt and the
  AppStore registry resolve binaries from OSS directly.

## Publishing a package (third-party developers)

Use `czdev publish` (from `m5stack/CardputerZero-AppBuilder`). With only a
normal GitHub login it will:

1. Upload your `.deb` as an asset on a Release **in your own fork** of this repo
   (your free quota, unlimited download bandwidth — costs the project nothing).
2. Open a PR here containing only `meta.json`, screenshots/icon, and a
   `<pkg>_<ver>_<arch>.deb.release.json` manifest with the fork release URL +
   sha256. No `.deb` enters git.

CI then:

1. **Validate** (`validate-pr.yml`, untrusted `pull_request` stage): downloads
   the `.deb` from the manifest URL, verifies sha256, runs `dpkg-deb` checks,
   maintainer/ownership and version checks; results are posted by the privileged
   `validate-pr-comment.yml` (`workflow_run`) stage.
2. **Promote on merge** (`update-index.yml`): pulls the approved `.deb` from the
   manifest URL into the official `apt-pool` release, rebuilds the index, deploys
   Pages, and syncs OSS.

### Automatic replacement of old versions

An app keeps only its **highest Debian version** in the store, across `arm64`
and `all` builds. A version update from either `czdev publish` or the portal
includes removal of older manifests in the same PR. Metadata-only updates do
not remove versions. Ordering uses Debian rules, including epochs, `~`
prereleases and numeric revisions; it never relies on upload time or filenames.

Deployment also selects the highest version from merged main, covering older
clients, historical multi-version apps and concurrently merged submissions.
Only that version is promoted and indexed. Older binaries are revoked after
both store registries exclude the retired versions **and contain the replacement**.
Any remaining superseded manifests are then removed from current main with a
non-forced push; concurrent changes and an intentional withdrawal of the
replacement are rechecked. Failed deployment or registry verification leaves
those fallback manifest deletions uncommitted and old Release assets intact.
Retry the deployment to resume an incomplete cleanup. Contributor-owned fork
releases remain under the contributor's control.

See [publishing lifecycle and channel comparison](docs/PUBLISHING.md).

If a PR is rejected, the project stored nothing — the binary only ever lived in
the contributor's own fork release.

## Withdrawing a version

Unpublish removes the requested version, keeping other published versions of
the app. When the last version goes, its listing, icon and screenshots go too.
The deployment rebuilds both APT indexes, `owners.json`, the worldwide
`generated/registry.json` and the OSS `cn/registry.json`; OSS's managed `pool/`
is synchronized with deletion enabled. After both store registries no longer
list withdrawn versions, their binaries are deleted from this repository's
`apt-pool`, `staging` and `czdev-buffer` releases, including portal-prefixed files.

Cleanup is derived from manifest deletions in main's full first-parent history,
so it also repairs old incomplete withdrawals and can be retried after a failed
deployment. Active manifest filenames are always protected. A repeated portal
request verifies the historical owner and retries the deployment without
opening a spurious failure issue. Run `update-index.yml` manually to retry an
incomplete cleanup; a failed registry check or deletion fails the workflow.

Upload downloads retry transient network/Release-CDN failures for up to ten
minutes using increasing delays. Downloads are written atomically, screenshots
and icons use the same retry helper, and integrity failures remain blocking.

Run regression checks with `python3 -m unittest discover -s tests -v` (Python
3.10+ and Node.js). They use temporary repositories and mocked downloads, and
never delete remote assets.

## Manifest format

`pool/main/<pkg>/<pkg>_<ver>_<arch>.deb.release.json`:

```json
{
  "filename": "nc2000_1.0.3_arm64.deb",
  "url": "https://github.com/<owner>/<repo>/releases/download/<tag>/nc2000_1.0.3_arm64.deb",
  "sha256": "…",
  "size": 20560444,
  "package": "nc2000",
  "version": "1.0.3",
  "architecture": "arm64"
}
```

Note: GitHub sanitizes `~` to `.` in release asset names, so `filename` may use
`.` where the Debian version uses `~` (e.g. `lofibox_0.2.0-1.lofibox23_arm64.deb`).

## OSS mirror

See `oss/README.md`. Enable by setting repo variable
`PACKAGES_OSS_SYNC_ENABLED=true` and the `OSS_ACCESS_KEY_ID` /
`OSS_ACCESS_KEY_SECRET` secrets.
