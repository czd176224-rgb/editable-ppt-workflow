# Editable PPT Workflow 2.0.0

Public Codex plugin for converting a paginated Word document plus an SVG Logo into an object-level editable 16:9 PowerPoint.

## V6 production contract

The production contract identifier is `word-ppt-workflow-v6`.

- One Word page becomes one slide.
- One global visual style is confirmed in the UI.
- The body is 1904x896 (17:8) inside a fixed 16:9 slide.
- Every Image2 body call uses `gpt-image-2 generate`; references never trigger `edit` or `--image`.
- Page comments may modify Word facts and request reference searches.
- Word images, attachments and search results are reference material only. Unavailable references are recorded and ignored without blocking.
- Light QA checks the effective page request, style, readability, 17:8 size and absence of generated fixed layers.
- Failed or non-improving later candidates fall back to the first valid candidate.
- Accepted bodies are reconstructed as editable objects; native title, SVG Logo, footer and page number are added afterward.
- Final validation is mechanical. OfficeCLI is optional.

The production dispatcher exposes V6 only. It does not migrate or resume V4/V5 state.

## Install

Download the immutable `v2.0.0` Windows release ZIP, verify its SHA-256 file, extract it and run `install.ps1`. Restart Codex after installation or upgrade.

Repository development and release instructions are in [docs/RELEASE.md](docs/RELEASE.md).

## Source

Repository: <https://github.com/czd176224-rgb/editable-ppt-workflow>

License and notices are included in `LICENSE` and `NOTICE`.
