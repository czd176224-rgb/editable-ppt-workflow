# 1.2.0 public release runbook

Release identity is fixed by `package-info.json`: version `1.2.0`, tag
`v1.2.0`. Never overwrite this tag or reuse this version.

1. Run `scripts/release_gate.ps1` on the reviewed private commit.
2. Push that commit only to the private authoritative repository and open a
   draft pull request.
3. Run `scripts/export_public_release.ps1` from the reviewed commit. It exports
   only Git-indexed allowlisted files and writes a source manifest.
4. Apply the snapshot to a fresh branch based on public `main`; do not merge or
   copy private Git history. Open a public pull request and wait for all checks.
5. After merge, verify the public merge tree matches the sanitized snapshot.
6. Create annotated tag `v1.2.0` on that exact public merge commit. The release
   workflow rejects a tag/version mismatch, reruns the release gate, creates a
   deterministic ZIP, and publishes its SHA-256 file.
7. In a clean unauthenticated directory, download the tag source and Release
   assets over HTTPS, verify `SHA256SUMS.txt`, inspect exclusions, run portable
   install/verify, and add the Marketplace using `--ref v1.2.0`.

Before publishing, create an immutable local marketplace preview named
`editable-ppt-local-preview-v110`, add
`editable-ppt-workflow@editable-ppt-local-preview-v110`, restart Codex, and
verify source/cache/runtime SHA-256 equality. The preview name must never be
reused for different bytes. Do not create the public tag until the live
four-page subscription acceptance and Office smoke checks pass.

Record the private PR URL/head SHA, public PR URL/merge SHA, tag SHA, Release
URL, archive SHA-256, and anonymous verification result in the release record.
