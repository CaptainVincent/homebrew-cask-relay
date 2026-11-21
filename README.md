# Homebrew Cask Relay

A Homebrew tap for ChromeDriver, dupeGuru, Min, RAR, and XLD. Homebrew's
main tap disabled these casks after they failed its Gatekeeper check.
Downloads still come from the original publishers. This tap does not change
Gatekeeper settings or remove quarantine. macOS may still block an app.

Preview: recipes and local checks are verified, but installs, upgrades, and
GitHub Actions have not been tested end to end.

## Install

Use a Homebrew version with `brew trust --cask` (the command is present in
Homebrew 7.0.15). Replace `YOUR_GITHUB_OWNER` with the repository owner:

```sh
tap=YOUR_GITHUB_OWNER/cask-relay
brew tap "$tap"
brew trust --cask "$tap/dupeguru"
brew install --cask "$tap/dupeguru"
```

Already installed from another tap? Use `brew reinstall --cask "$tap/dupeguru"`
instead. To update, run `brew upgrade`. For self-updating casks such as Min
and XLD, use `brew upgrade --cask --greedy` with the full cask name.

To trust the entire supported list without trusting the tap itself, run
[`scripts/trust-casks`](scripts/trust-casks) from this checkout to preview the
commands, then rerun with `--apply`. It does not install or upgrade anything.

See the [cask list](casks.txt) and [maintenance notes](docs/maintenance.md).
Based on [homebrew-attic](https://github.com/ylluminate/homebrew-attic).
[BSD-2-Clause](LICENSE.md).
