# 2.0.1 public release runbook

Release identity is fixed by `package-info.json`: version `2.0.1`, tag `v2.0.1`. Never overwrite the tag or reuse the version.

1. Merge all milestone PRs into `main` with green bounded Windows CI.
2. Run `scripts/release_gate.ps1` locally, including the portable clean-install smoke.
3. Refresh and verify the public source manifest and release audit.
4. Create annotated tag `v2.0.1` on the exact merge commit and push it.
5. The release workflow repeats the full gate, builds the reproducible Windows ZIP and publishes the GitHub Release.
6. Install `editable-ppt-workflow@editable-ppt-public`, restart Codex and verify the installed plugin reports `2.0.1`.

The tag workflow retains the portable clean-install smoke; pull-request CI intentionally skips that external installation step and has a bounded timeout.
