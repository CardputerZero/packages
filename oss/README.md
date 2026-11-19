# OSS Sync

This directory contains the optional Aliyun OSS sync path for the generated
CardputerZero APT repository.

The normal publish path remains GitHub Pages. OSS sync is off by default and
only runs when explicitly enabled.

## What It Uploads

The workflow builds the APT repository into `public/`, then this sync uploads
the generated files to:

```text
oss://cardputer-zero-repo/packages/
```

That keeps the same path shape as the GitHub Pages URL:

```text
https://cardputerzero.github.io/packages/
```

When syncing to `oss-cn-shenzhen.aliyuncs.com`, the script also generates an
AppStore registry for the China region at:

```text
https://cardputer-zero-repo.oss-cn-shenzhen.aliyuncs.com/packages/cn/registry.json
```

The CN registry is generated from the OSS sync tree, so it can differ from the
default registry published at:

```text
https://cardputerzero.github.io/generated/registry.json
```

## Incremental Uploads

Each file is compared with the destination object's CRC64-ECMA checksum using
`ossutil stat` and `ossutil hash`. Matching contents are skipped, including when
a fresh checkout or download changes the local modification time. New files,
changed contents and objects without a CRC64 header are uploaded. A retry checks
the objects already uploaded by the previous attempt and skips matching contents.
Pool files are checked and uploaded with eight workers (configurable through
`OSS_SYNC_WORKERS`, from 1 to 32). A pool failure blocks all catalog publication.
Once all pool operations succeed, repository metadata is updated sequentially:
Packages before Release, and the CN registry last.

The configured credentials need `oss:GetObject` and `oss:GetObjectAcl` for the
metadata queries made by ossutil 1.7.7, in addition to `oss:PutObject`.
A missing object is uploaded; other metadata query errors stop the sync.

Before uploading, `prune-pool.py` lists the managed `pool/` prefix and removes
objects absent from the local pool, preserving version withdrawals and region
exclusions. Listing requires `oss:ListObjects`, and cleanup requires
`oss:DeleteObject`, as the previous `ossutil sync --delete` did. All listing
pages are validated before deleting anything. A listing or deletion failure
stops deployment before publishing a new catalog. An empty pool clears the
managed prefix. Objects outside `pool/`, including OS images and other
repositories, are retained. The remaining pool objects use the same CRC64
comparison as the indexes, so cleanup does not re-upload unchanged packages.

## Tests

The regression tests run the real ossutil CLI against a local HTTP server with
temporary credentials and objects. They require Python 3.10+, Bash, ossutil 1.7.7,
`dpkg-deb`, and `dpkg-scanpackages` from `dpkg-dev`.

From the repository root:

```bash
OSSUTIL_TEST_BINARY=/path/to/ossutil python3 -m unittest discover -s oss -p 'test_*.py' -v
```

The suite covers repeated uploads after modification-time changes, same-size
content changes, missing objects, interrupted uploads, metadata failures, empty
files, the default destination prefix, the China region filter, withdrawals,
empty pools, paginated listings, and listing/deletion failures. These tests run
in the Test Store Pipeline workflow without production OSS credentials.

## Enable It

Manual run:

1. Run the `Deploy APT Repository to Pages` workflow.
2. Set `sync_oss` to `true`.

Automatic runs on `main` pushes:

1. Set repository variable `PACKAGES_OSS_SYNC_ENABLED` to `true`.
2. Keep the required OSS secrets configured.

## Required Secrets

```text
OSS_ACCESS_KEY_ID
OSS_ACCESS_KEY_SECRET
```

## Optional Variables

```text
PACKAGES_OSS_ENDPOINT=oss-cn-shenzhen.aliyuncs.com
PACKAGES_OSS_BUCKET=cardputer-zero-repo
PACKAGES_OSS_PREFIX=packages
PACKAGES_OSS_PUBLIC_BASE_URL=https://cardputer-zero-repo.oss-cn-shenzhen.aliyuncs.com
```

`PACKAGES_OSS_PREFIX` may be empty if the repository should be uploaded at the
bucket root, but the default `packages` prefix avoids collisions with OS image
files and `os-list.json`.

`PACKAGES_OSS_PUBLIC_BASE_URL` is optional. If it is empty, the sync script uses
`https://${PACKAGES_OSS_BUCKET}.${PACKAGES_OSS_ENDPOINT}`.

## Per-Package Region Gate

Each package can opt out of the China OSS mirror in its `meta.json`:

```json
{
  "region": {
    "default": true,
    "CN": false
  }
}
```

When the target endpoint is `oss-cn-shenzhen.aliyuncs.com`, packages with
`region.CN` set to `false` are removed from the temporary OSS sync tree before
upload. Their `.deb`, icon, screenshots, and metadata are not uploaded, and the
OSS-side APT index is rebuilt without those packages.

If `region` is missing, or `region.CN` is missing, the package is allowed to
sync by default. Other region keys are reserved for future mirrors.
