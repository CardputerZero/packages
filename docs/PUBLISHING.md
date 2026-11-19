# Publishing lifecycle

Each app retains its highest Debian version. Both `arm64` and `all` manifests
may remain at that same version; older versions are removed even when an app
changes architecture. Listing assets belong to the app and survive an update.

| Stage | czdev CLI | Web upload |
| --- | --- | --- |
| Upload | Contributor's fork `czdev-buffer`, or official repo for maintainers | Official `staging` Release with login-prefixed names |
| Upload limit | CLI permits up to 2 GiB per Release asset | Worker defaults to 80 MB, configurable by `MAX_SIZE_MB` |
| Submit | PR based on current upstream main, with metadata and a release manifest | Worker dispatches `web-submission`; trusted workflow creates the PR |
| Validate | Read-only fork PR checks; policies read from PR base | Trusted workflow validates downloaded `.deb` and store metadata |
| Identity | PR author must match `uploaded_by` and own the app | Authenticated Worker login; admin override is supported |
| Version | Must exceed the highest manifest version on PR base | Must exceed the highest manifest version on checked-out main |
| Replace | Publish PR removes strictly older manifests | Publish PR removes strictly older manifests using `catalog_cleanup.py supersede` |
| First release | Human review and device demo | Human review and device demo |
| Existing app update | Privileged follow-up merges after checks | Trusted workflow merges after checks and explicitly dispatches deployment |
| Store listing only | `czdev publish --meta-only` | `web-listing-update`; neither entry point adds version deletions |
| Deploy and cleanup | Shared `update-index.yml` | Shared `update-index.yml` |

The browser and CLI prechecks provide early feedback; the server-side checks
remain authoritative. In particular, index/CDN caching can make a precheck
stale. GitHub's bot token does not trigger a push workflow after merging, so
bot paths explicitly dispatch deployment.

## Shared deployment

1. Read merged main and plan historical withdrawals plus superseded versions.
   Choose the highest version per app with `dpkg --compare-versions`.
2. Stage fallback deletions locally. Promote and collect only selected manifests;
   verify the replacement package, rebuild APT indexes and ownership data, sync
   OSS and deploy Pages. The existing withdrawal pipeline removes retired OSS
   objects as part of synchronization and rebuilds the two store registries.
3. Require the global and China registries to exclude withdrawn versions and
   include active replacements. An empty or stale registry cannot authorize
   removal of a replacement's older Release binary.
4. Persist remaining superseded manifest deletions on current main. Recheck
   manifest contents and that a newer version still exists, then push without
   force. Retry concurrent pushes; never overwrite a publisher's work.
5. Delete exact withdrawn filenames from official `apt-pool`, `staging` and
   `czdev-buffer` Releases. Protect active filenames in the deployed snapshot
   and current main. Contributor fork Releases are not managed by this job.

This backend selection also handles an old client or a stale PR reintroducing
an obsolete manifest. Deployments are serialized and cleanup is retryable. A
failure stops subsequent steps; rerunning deployment recomputes the plan from
main and its full history. No remote deletion occurs while a publish PR is
merely open.

## Related repositories

- `CardputerZero/packages` owns the live workflows and deployment policy.
- `CardputerZero/dev-portal/packages-workflows` mirrors their source templates.
- `CardputerZero/AppBuilder/scripts/czdev/publish.py` stages the matching CLI
  deletions. Existing CLI versions remain covered by backend selection.

Deploy the packages changes before relying on the portal's new lifecycle text.
