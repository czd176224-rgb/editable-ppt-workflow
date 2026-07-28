# Editable PPT Workflow

This plugin accepts one user-supplied paginated Word document and produces one editable PowerPoint slide per locked Word page.

## Workflow

`one Word input → marker-first/physical-fallback pagination → one embedded three-stage browser confirmation phase → immutable shared style contract → independent page image generation → relaxed page-local QA and repair → independent reconstruction → mechanical final assembly`

The three-stage browser interaction is embedded in `word-to-editable-ppt` and adapted from the PPT Master interaction model. The plugin does not depend on an external package, install, import, or filesystem path for that interaction.

Initial page creation uses exactly one `images/generations` request with only the current-page text, exact frozen style execution, and technical output parameters. One combined page-QA decision checks overall style plus source content and logic. Passing pages do not enter a second visual review. Local fixes to an existing page may use `images/edits`; only structural, logic, or material overall-style failures generate a fresh image.

The single browser phase freezes detailed visual controls (including formal/modern/minimal degree, a four-level type scale, density, and free-form requirements) and production controls (quality, concurrency, repair budget, editable output, and start). `ppt169` uses `1792x1008` with a 16:9 PowerPoint canvas; `ppt43` uses `1536x1152` with a 4:3 canvas. Both mappings are no-crop.

Pages have independent scheduler, QA, retry, cache, and reconstruction states. A passing page may reconstruct while other pages generate or repair. Strict project-local hashes reuse unchanged page images, QA receipts, editable packages, and final render evidence. The final writer waits for all locked pages, assembles them in Word order, and checks count/order, artifact integrity, page-QA status, editability, package validity, and open/back-render capability.

Microsoft Word and PowerPoint are preferred when installed. LibreOffice is a lazy optional fallback for unmarked physical pagination and final back-rendering; it is not required when the corresponding Microsoft Office capability is available. If neither renderer exists, structural PPTX validation remains non-blocking and the final report records a clear advisory instead of spending generation quota or stopping installation.

The only generated-image directory is `06_images/generated/`. Final deliverables and their mechanical receipts are written only to `08_final/`.

## Deterministic routing

| Skill | Owns | Boundary |
| --- | --- | --- |
| `word-to-editable-ppt` | One-Word preparation, embedded browser confirmation, immutable style contract, independent page state, relaxed QA, and final mechanical checks. | It does not add pages outside the Word source or require other user uploads. |
| `codex-gpt-image` | Initial page generation and issue-specific image repair with normalized provenance. | It does not decide final page acceptance or assemble PPTX files. |
| `image-to-editable-ppt` | Per-page editable reconstruction, strict page cache, and one-writer locked-order assembly. | It consumes accepted current-page artifacts only. |
| `officecli` | Validation and surgical fixes on the assembled deliverable. | It does not own generation, page QA, or assembly. |

The latest supported build is distributed through the public `editable-ppt-public` Marketplace snapshot. Development changes remain private until they pass validation and are exported as a clean public release.
