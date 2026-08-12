# 2.1.0 public release runbook

Release identity is fixed by `package-info.json`: version `2.1.0`, tag `v2.1.0`, workflow `word-ppt-workflow-v6`, prompt contract `page-prompt-v6-adaptive-confirmed-materials`, and policy `generate-without-refs-edit-with-confirmed-refs`. Never overwrite the tag or reuse the version.

The single final UI submission is the sole material/reference authority; every staged reference requires explicit keep/remove. Zero references uses generate and 1–16 confirmed refs uses edit. Fidelity is high-fidelity best effort, never pixel-perfect. QA outage falls back to candidate1 marked unvalidated. Wrong-size outputs are rejected rather than stretched or cropped. Fixed title/original SVG logo/footer/page number are PPT layers. There is no V4/V5 runtime fallback, exact overlay, or post-reconstruction visual repair. Reconstruction may require separate editppt authentication; `401 token_expired` is an external credential failure, not a successful reconstruction.

1. Merge all milestone PRs into `main` with green bounded Windows CI.
2. Run `scripts/release_gate.ps1` locally, including portable smoke where the environment permits.
3. Refresh and verify the public source manifest and release audit.
4. Create annotated tag `v2.1.0` on the exact reviewed merge commit and push it.
5. The release workflow repeats the gate, builds the deterministic Windows ZIP and publishes the GitHub Release.
6. Install `editable-ppt-workflow@editable-ppt-public`, restart Codex and verify the installed plugin reports `2.1.0`.
