---
name: generate-slide-body-image
description: Use when a V6 page has sealed confirmed materials and needs an adaptive gpt-image-2 body candidate through Codex authentication.
---

# Generate Slide Body Image V6

Use this skill only for a page prepared by `run-word-to-ppt-workflow`. Consume its sealed prompt and verified reference list without adding facts or reinterpreting comments.

## Adaptive operation contract

- With zero valid confirmed references, call `codex_gpt_image.py generate` and pass no image inputs.
- With one to sixteen valid confirmed references, call `codex_gpt_image.py edit` with aligned `--image`, `--image-role`, and `--image-sha256` values. The edit subcommand requires at least one `--image`.
- A retry keeps the same operation and same original confirmed references, never candidate 1.
- Use `medium` for ordinary pages and `high` for Logo, screenshot, dense-data, small-text or high-detail risk. Produce at most two candidates.
- Treat Logo, screenshot and real-photo fusion as high-fidelity best effort; do not promise exact reproduction.
- The output is the 1904x896, 17:8 body. Do not draw the fixed page title, SVG Logo, footer or page number.

Return the output and trace to the orchestrator. If QA is unavailable or a later candidate fails or does not improve, preserve the first valid candidate. Do not use `OPENAI_API_KEY`, print OAuth tokens, invent an independent prompt, or block on unavailable references.

For CLI parameters, read `references/openai-images-api-parameters.md`.
