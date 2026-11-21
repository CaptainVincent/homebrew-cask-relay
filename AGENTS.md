# Local tap boundaries

- Only tokens explicitly listed in `casks.txt` may be managed.
- Preserve official publisher sources and all unrelated code. Version/URL/hash
  updates must come from verified official recipes or native Homebrew bump.
  Strictly newer version/hash-only updates may use the tested routine policy;
  every other change needs owner review. Only the Gatekeeper-only disable exception is allowed; never add
  quarantine-removal hooks.
- Other disable/deprecation reasons and source drift fail closed.
- Generated files must match `source-lock.json`; do not overwrite local edits.
- Run the offline unit suite after code changes. Network/native bump tests must
  be described separately; never claim fixture success proves an app installs.
- Do not publish, register/trust taps, install/reinstall apps, restart services or
  enable hosted workflows without explicit owner scope for those effects.
- `--bump` downloads app archives. Respect its storage preflight and preserve
  native audits; do not add broad skip/force flags to make a failure pass.
