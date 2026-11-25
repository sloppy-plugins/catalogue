# Sloppy Plugin Catalogue

Add `https://github.com/sloppy-plugins/catalogue` in **Sloppy Settings → Plugins → Add Catalogue…**, then refresh and browse available plugins.

This repository is standalone. Its `catalogue.json` lists public plugin repositories,
exact Git commits or registry archives with SHA-256 hashes, and complete version-specific manifests. The `catalogue.public.json` seed is empty; public releases are added only after
plugin releases have been published to their configured Git, npm or PyPI source.
It never advertises a release with a placeholder or unpublished commit.

## Maintain releases

Requirements: Python 3.11+, Git, Make and the public `sloppy-sdk==0.1.2` package. Publish
the SDK on PyPI before installing the catalogue's default tooling dependencies.

```sh
make deps
make check
```

To test the tools before the SDK is published, clone its standalone repository
separately and run `make deps SDK_SOURCE=/path/to/sdk/python`.

After committing and pushing plugin changes to GitHub, put their checkouts beside
this repository (or set `PLUGIN_ROOT=/path/to/checkouts`):

```sh
make sync-check
make sync
git diff -- catalogue.json
# Review, commit and push the catalogue yourself.
```

Synchronization selects the locations in `sources.json`. Git sources verify
clean working trees and fresh GitHub `origin/main` commits. Registry sources
read the published archives without running plugin code, verify registry integrity,
extract the inline manifest and pin the archive URL and SHA-256. Both use the SDK's
catalogue validator. It validates every candidate before
writing the catalogue atomically. It never commits, stages, or pushes changes.
A known version retains its original commit even when its branch advances. Changing
a published manifest without a version bump is rejected. Existing releases remain unchanged.

The SDK's `sloppy-catalogue add-plugin`, `add-version`, `remove-version` and `validate`
commands remain available for manual maintenance. Publish the SDK first, then
plugin commits, then the catalogue that references those commits. Sloppy checks
runtime requirements and prepares installed plugins in their own private copies.

## Source locations and automatic updates

`sources.json` maps plugin IDs to their locations. Add a plugin here rather than
editing the workflow. Supported location shapes are:

```json
{
  "sources_version": 1,
  "plugins": {
    "example-git": {
      "kind": "git",
      "repository": "https://github.com/owner/plugin.git"
    },
    "example-node": { "kind": "npm", "package": "@scope/plugin" },
    "example-python": { "kind": "pypi", "project": "example-plugin" }
  }
}
```

The update workflow checks locations hourly and can be run manually with **Run
workflow** or a `plugin-updated` repository dispatch. Git locations use the committed
manifest on `main` and stable version tags (`vX.Y.Z` or `X.Y.Z`); tags retain
versions published between scheduled checks. Bump the manifest version when publishing a new plugin version.
The workflow opens or updates one PR containing validated catalogue changes.
It never runs plugin code or dependency initialization. Enable **Allow GitHub Actions
to create and approve pull requests** in repository Actions settings to permit PR creation.

The current public plugins use npm (`@sloppy-plugins/codex` and
`@sloppy-plugins/copilot`) and PyPI (`sloppy-testing`). Stable non-deprecated npm
versions and non-yanked PyPI distributions are eligible. Universal Python wheels
are preferred, with source archives as a fallback. The archive must contain one
plugin manifest matching the registry package/project identity and version.
Unsafe paths, links, ambiguous manifests, hash mismatches and attempts to change
an existing release fail the entire update without publishing partial results.
The discovery artifact retains the full registry inventory, including prereleases
and yanked status. Sloppy installs the pinned archives into private plugin storage,
checks the actual manifest, and performs the manifest's declared initialization.

Publish SDK 0.1.2 before enabling this workflow, since its catalogue validators add
registry source support. For local validation before publication, use the
standalone SDK source override described above.

Discovery can also be run locally with Python and Git:

```sh
python3 scripts/discover.py --destination /tmp/plugin-checkouts --report /tmp/discovered-versions.json
make sync PLUGIN_ROOT=/tmp/plugin-checkouts
```

The destination must be empty. Missing sources or registry/network errors stop the
update before catalogue writes. Public SDK installation must be available on PyPI
before the automated jobs can run successfully.
