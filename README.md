# Editable PPT Workflow 2.1.0

Public Codex plugin for converting a paginated Word document plus an SVG Logo into an object-level editable 16:9 PowerPoint.

## V6 adaptive production contract

The workflow contract is `word-ppt-workflow-v6`; the prompt contract is `page-prompt-v6-adaptive-confirmed-materials`.

- One Word page becomes one slide. The body is 1904x896 (17:8).
- The single final UI submission is the sole material/reference authority. Every staged reference requires explicit keep/remove; the backend cannot reinterpret it afterward.
- Zero confirmed references uses Image2 `generate`; 1–16 confirmed refs uses `edit`, preserving their ordered role descriptions.
- Reference fusion is high-fidelity best effort, never a pixel-perfect guarantee.
- QA outage is nonblocking: candidate1 is used and explicitly marked `unvalidated`.
- Provider outputs with the wrong dimensions are rejected rather than stretched or cropped.
- Fixed title, original SVG logo, footer and page number are PPT layers and never Image2 body content.
- V6 has no V4/V5 runtime fallback, exact overlay, or post-reconstruction visual repair.
- Object-level reconstruction may require separate `editppt` authentication.

## Install

Download the immutable `v2.1.0` Windows release ZIP:

`https://github.com/czd176224-rgb/editable-ppt-workflow/releases/download/v2.1.0/editable-ppt-workflow-2.1.0-windows.zip`

Download the adjacent `SHA256SUMS.txt`, verify locally with `Get-FileHash`, extract the ZIP and run `install.ps1`. Restart Codex after installation or upgrade.

Repository development and release instructions are in [docs/RELEASE.md](docs/RELEASE.md).

## Source

Repository: <https://github.com/czd176224-rgb/editable-ppt-workflow>

License and notices are included in `LICENSE` and `NOTICE`.
