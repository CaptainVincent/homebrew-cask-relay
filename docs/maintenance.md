# Maintenance

## Local checks

Run from the repository root with Python 3.11 or newer and Ruff 0.17.5.
Ruby is needed to write a recipe; native version bumps also need Homebrew.

```sh
ruff check cask_relay tests
ruff format --check cask_relay tests
python3 -m unittest discover -s tests
python3 -m cask_relay sync --dry-run   # fetches recipe metadata, not installers
python3 -m cask_relay sync             # refreshes Casks/ and source-lock.json
python3 -m cask_relay policy           # classifies changes; does not publish
```

Add supported casks to `casks.txt` and commit the list and generated files together.
The sync refuses to remove unlisted files automatically: move any installed users
first, then remove the recipe and its lock entry. Do not hand-edit generated
files without updating the lock. A publisher change, an unknown disable reason or
a missing upstream recipe stops the sync. Check Git status after an interrupted run.

## Updates

The sync fetches Homebrew recipes by commit and verifies their source hashes. It
removes only the matching `fails_gatekeeper_check` disable line, not quarantine
metadata. If the publisher has a newer release, Homebrew `livecheck` and
`bump-cask-pr --write-only` can update the local recipe. `--bump` downloads archives
but does not install apps. Run it from the registered checkout with
`python3 -m cask_relay sync --tap YOUR_GITHUB_OWNER/cask-relay --bump`.

Pushes to `main` and pull requests run the read-only checks. The maintenance
workflow runs on the default branch. Start it manually in Actions, or set
`CASK_RELAY_ENABLED` to `true` to schedule it daily. It opens a pull request for any accepted
change, including routine bumps. It never pushes to the default branch. An
open update PR blocks new ones. One failed version check stops the batch.
If a native bump changes more than the routine policy allows, the job reports
the cask, versions and a bounded diff, then rolls back without opening a PR.

The maintenance job runs tests before opening its PR. The job needs contents
and pull-request write access, and repository settings must allow Actions to
open a PR. A separate job then starts the read-only Checks workflow on the
published commit using `workflow_dispatch`. Only that job needs `actions: write`;
no additional token is required. Use the dispatched Checks result on the PR's
head commit: GitHub may still show an approval prompt for the separate
`pull_request` run. Preparation and publication share a runner; the diff check
is not a sandbox. Review the recipe and lock changes before merging.

Leave `CASK_RELAY_ENABLED` unset until a manual maintenance run succeeds. Verify an install, a tap
migration, an upgrade, and a maintenance run in a test repository before
advertising them as supported. The local tests use fixtures for Homebrew's
networked operations.

To include Min and XLD in plain `brew upgrade`, add their names to
`HOMEBREW_UPGRADE_GREEDY_CASKS` in Homebrew's `brew.env`, keeping any existing
names. With the default config path `~/.homebrew/brew.env`, the line is
`HOMEBREW_UPGRADE_GREEDY_CASKS=min xld` (no quotes or `export`).

Upstream base: `ylluminate/homebrew-attic` at
`622231edffc2fec22c0305d9f05362396945e6c0`. Keep the BSD attribution and
publish to your own remote, never to the upstream repository.
