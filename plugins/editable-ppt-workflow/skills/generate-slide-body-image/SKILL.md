---
name: generate-slide-body-image
description: Generate or repair the complete 17:8 slide-body image for every page in the editable PPT workflow with gpt-image-2 and Codex authentication. Use when the workflow requests a page body from sealed Word content, comments, style, and approved reference materials.
---

# Generate Slide Body Image

Use this skill as the plugin's Image2 execution layer. Generate or edit the complete page-body image through Codex OAuth instead of the OpenAI API-key path. The bundled CLI reads the local Codex auth file and calls the Codex Images backend endpoints.

## Role boundary

Use this skill only after `run-word-to-ppt-workflow` has produced a sealed page-generation work item. The work item must identify the locked Word page, page-local comments, normalized UI style contract, approved page-image roles, bounded attachment/search evidence, production profile, output path, and trace path. If that authority is missing, stop and route the request through the workflow orchestrator. Do not use this role for standalone or general-purpose image generation.

Every initial page body and every bounded issue-targeted repair uses `gpt-image-2` through Codex OAuth. Do not use the official Images API, an OpenAI-compatible gateway, or API-key billing in this skill.

Hard boundaries:

- Generate the complete body region only. Never draw the fixed page title, actual SVG Logo, footer, footer line, or page number, even when a comment or reference image contains them.
- Target 17:8. The decoded output must satisfy `abs((width / height) / (17 / 8) - 1) <= 0.01`; otherwise return a repairable ratio failure or block after the configured repair budget.
- Treat Word as the factual and narrative authority. Comments specify page-local generation requirements; the normalized UI contract specifies global visual style.
- Treat Word page images as references unless the sealed work item marks an identified image as required presence. Use only the page-local, hash-verified inputs listed in the work item.
- Never add unsupported claims, unrelated attachment content, a UI audit screenshot, full unbounded attachments, watermarks, or extra visible text not authorized by the sealed page task.

## Core Workflow

All CLI commands below assume the working directory is this skill folder. Otherwise, resolve the absolute path to `scripts/codex_gpt_image.py` from this `SKILL.md`.

1. Check local Codex auth:

   ```bash
   python3 scripts/codex_gpt_image.py auth-status
   ```

2. If Codex auth is missing, run the device-code login flow:

   ```bash
   python3 scripts/codex_gpt_image.py login --open-browser
   ```

   The CLI prints a browser URL and a short user code. The user must complete this step; never ask them to paste tokens.

3. Generate the page body with the orchestrator-compiled prompt and exact production-profile size. Do not substitute an independently written prompt.

4. Edit or use reference images by passing only the `--image` inputs sealed by the orchestrator. For repairs, use the orchestrator-compiled repair prompt and preserve every unchanged invariant. Pair each input with its sealed `--image-role <role>` and write `--trace-out <trace.json>`; the trace records the endpoint, model, Codex OAuth mode, input hashes, and output hashes.

5. Write the output and trace to the sealed destinations. Report the saved path, model, requested and decoded dimensions, input roles/hashes, output hash, and Codex OAuth mode. The workflow independently enforces the 17:8 relative-error boundary; never stretch or contain-pass a wrong-ratio result.

## Defaults

- Auth file: `~/.codex/auth.json`
- Override auth file: `CODEX_AUTH_FILE=/path/to/auth.json`
- Login fallback: `login` uses OpenAI Codex device-code auth and writes the same auth file
- Login client id: `--client-id`, `CODEX_APP_SERVER_LOGIN_CLIENT_ID`, then the public Codex default
- Images base URL: `https://chatgpt.com/backend-api/codex`
- Image model: `gpt-image-2`
- Size: `auto`
- Quality: `auto`
- Background: `auto`
- Moderation: `auto`
- Output format: `png`
- Output compression: `100` for `jpeg` and `webp`

For detailed parameter values, defaults, model-specific constraints, and CLI mapping, read `references/openai-images-api-parameters.md`.

## Parameter Selection

Use the orchestrator's production-profile parameters. Read the reference only to map those sealed values to explicit `model`, `size`, `quality`, `background`, `moderation`, and `output_format` flags; do not reinterpret them from a standalone request.

## Prompting

Use only the compiled page prompt from the sealed work item. Preserve its content authority, material roles, style contract, fixed-layer exclusions, and repair target. A repair prompt may change only the recorded failing issue. Never independently add a Logo, title, footer, page number, watermark, unsupported claim, or unrelated material.

## Failure Handling

- Missing auth file: run `codex_gpt_image.py login --open-browser`, or ask the user to run `codex login`, then retry.
- 401/403: the Codex OAuth token may be expired, the account may not have access, or the endpoint may reject the session. Ask the user to refresh Codex auth.
- Network failures: retry once if the request is idempotent and the user accepts possible duplicate image generation.
- Never print or paste tokens from `~/.codex/auth.json`.

## Implementation Notes

The CLI sends Codex Images requests with:

- generation endpoint: `POST https://chatgpt.com/backend-api/codex/images/generations`
- edit endpoint: `POST https://chatgpt.com/backend-api/codex/images/edits`
- auth: `Authorization: Bearer <access token from ~/.codex/auth.json>`
- model: `gpt-image-2`

It parses the JSON Images response and writes returned base64 image payloads to local files.
