# Security and privacy

## Data handling

The plugin keeps project state and incremental caches inside the project selected for the current run. It does not intentionally use one project's generated content as memory for another project.

Image generation sends the current page's text prompt and only the task-local images required for that page to the configured image backend. OCR can send task-local page images to the configured OCR provider when an online provider is enabled. Review your organization's data policy before processing confidential documents.

The repository and release packages must not contain API keys, Codex authentication files, user documents, generated presentations, project caches, or machine-specific user paths.

## Credentials

Do not commit credentials. Configure authentication through Codex login or the supported local configuration commands. The installer never asks users to paste a GitHub or OpenAI token into this repository.

## Reporting a vulnerability

Use GitHub's private vulnerability reporting feature for this repository. Do not include real confidential documents or credentials in a report; use a minimal synthetic reproduction.

## Supported release

Security fixes target the latest published release. Older cachebuster builds are not maintained.
