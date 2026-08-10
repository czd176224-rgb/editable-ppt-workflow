---
name: generate-slide-body-image
description: Generate the complete 17:8 slide-body image for V6 pages with gpt-image-2 generate-only and Codex authentication.
---

# Generate Slide Body Image V6

Use this skill only for a page prepared by `run-word-to-ppt-workflow`. Every candidate, including a QA-targeted retry, is a fresh `gpt-image-2` generation through Codex OAuth.

## Non-negotiable operation contract

- Always call `codex_gpt_image.py generate`.
- Never call `edit` and never pass `--image` for a V6 body.
- Word images, attachments and searched materials are references for understanding or design; they never change the operation to edit and never require exact pixel reproduction.
- Generate only the 1904x896, 17:8 body. Do not draw the fixed page title, SVG Logo, footer or page number.
- Page comments are authoritative and may modify Word facts. Without comments, preserve the complete page Word text.
- Use only the orchestrator-compiled prompt, which includes effective page content, global style, available reference descriptions, geometry and fixed-layer exclusions.

## Execution

1. Check auth with `python scripts/codex_gpt_image.py auth-status`. Auth resolution is `CODEX_AUTH_FILE`, then `$CODEX_HOME/auth.json`, then `~/.codex/auth.json`.
2. Run `generate` with the sealed prompt file, `--model gpt-image-2`, `--size 1904x896`, sealed output path and `--trace-out`.
3. Return the output and trace. The orchestrator performs light QA and may request another fresh generation.
4. If later candidates fail or do not improve, preserve and select the first valid candidate as directed by V6.

Do not use `OPENAI_API_KEY`, print OAuth tokens, add an independent prompt, or treat unavailable references as blockers.

For supported CLI parameters, read `references/openai-images-api-parameters.md`.
