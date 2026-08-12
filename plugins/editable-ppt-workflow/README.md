# Editable PPT Workflow

`word-ppt-workflow-v6` creates a new V6 project from one paginated Word document and one SVG Logo. Comments are resolved before the one Confirm UI into effective body edits, attachment extraction, and real-reference/image requirements. Pending real-reference acquisition runs once; inaccessible inputs are recorded without blocking.

The UI final stage shows editable per-page materials and safe thumbnail, original and model-input controls. On submit, the sealed result is the only prompt and QA authority.

Image2 selection is adaptive: zero valid confirmed references uses `generate`; one to sixteen valid confirmed references uses `edit`, never an empty edit. Ordinary pages use `medium`, risk pages use `high`; retries reuse the same original references, never candidate 1. The workflow uses at most two candidates, bounded concurrency and nonblocking fallback.

Every Image2 body is the 1904x896 (17:8) body region inside a 16:9 slide. The fixed page title, original SVG Logo, footer and page number are native PPT layers outside Image2. Reference fusion is high-fidelity best effort. There is no post-generation exact overlay, no post-reconstruction visual repair or comparison, and no V4/V5 runtime fallback.

Use `run-word-to-ppt-workflow` as the production orchestrator.
