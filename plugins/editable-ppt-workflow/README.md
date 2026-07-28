# Editable PPT Workflow

This plugin accepts one user-supplied paginated Word document and produces one editable PowerPoint slide per locked Word page.

## Workflow

`one Word input → marker-first/physical-fallback pagination → one embedded three-stage browser confirmation phase → immutable shared style contract → independent page image generation → relaxed page-local QA and repair → independent reconstruction → mechanical final assembly`

The three-stage browser interaction is embedded in `word-to-editable-ppt` and adapted from the PPT Master interaction model. The plugin does not depend on an external package, install, import, or filesystem path for that interaction.

Initial page creation uses `images/generations` with only the current-page text, exact frozen style execution, and technical output parameters. Local fixes to an existing page may use `images/edits`; structural, logic, or material overall-style issues generate a fresh image.

The single browser phase freezes detailed visual controls (including formal/modern/minimal degree, a four-level type scale, density, and free-form requirements) and production controls (quality, concurrency, repair budget, editable output, and start). `ppt169` uses `1792x1008` with a 16:9 PowerPoint canvas; `ppt43` uses `1536x1152` with a 4:3 canvas. Both mappings are no-crop.

Pages have independent scheduler, QA, retry, cache, and reconstruction states. A passing page may reconstruct while other pages generate or repair. The final writer waits for all locked pages, assembles them in Word order, and checks count/order, artifact integrity, page-QA status, editability, package validity, and open/back-render capability.

The only generated-image directory is `06_images/generated/`. Final deliverables and their mechanical receipts are written only to `08_final/`.

## Deterministic routing

| Skill | Owns | Boundary |
| --- | --- | --- |
| `word-to-editable-ppt` | One-Word preparation, embedded browser confirmation, immutable style contract, independent page state, relaxed QA, and final mechanical checks. | It does not add pages outside the Word source or require other user uploads. |
| `codex-gpt-image` | Initial page generation and issue-specific image repair with normalized provenance. | It does not decide final page acceptance or assemble PPTX files. |
| `image-to-editable-ppt` | Per-page editable reconstruction, strict page cache, and one-writer locked-order assembly. | It consumes accepted current-page artifacts only. |
| `officecli` | Validation and surgical fixes on the assembled deliverable. | It does not own generation, page QA, or assembly. |

The latest supported build is distributed through the public `editable-ppt-public` Marketplace snapshot. Development changes remain private until they pass validation and are exported as a clean public release.
