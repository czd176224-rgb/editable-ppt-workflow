# Paginated Word to editable PowerPoint V6

A new V6 project is created from the original paginated Word and SVG Logo. Before one Confirm UI opens, comments become effective body edits, attachment extraction instructions, and concrete real-reference/image requirements. Attachment and reference failures are nonblocking; pending reference acquisition runs once and ends as confirmed, rejected, or `failed_no_retry`.

The final UI stage exposes editable page materials plus thumbnail, original and model-input media controls. After submission, the sealed result is the only prompt and QA authority; production never reinterprets comments or silently rewrites confirmed material.
Every acquired image, including one whose bytes were custody-confirmed earlier, requires an explicit keep/remove decision in this single final submission. Only kept images enter the frozen result. The final submission cannot be reopened or submitted twice.

For every 1904x896 body, zero valid confirmed references selects `generate`; one to sixteen valid confirmed references selects `edit`. Empty edit is forbidden. A retry uses the same original references, never candidate 1. Ordinary quality is `medium`; risk pages use `high`. The budget is at most two candidates with bounded concurrency and nonblocking fallback.
If semantic QA is unavailable, generation does not pause: candidate 1 may be selected as `accepted_fallback_first`, with `qa_unavailable` recorded explicitly. This is a degraded nonblocking result, not a semantic pass and not proof that invented visual facts were checked.

The fixed page title, original SVG Logo, footer and page number remain outside Image2 and are added only as fixed PPT layers. Real-photo, screenshot and Logo fusion is high-fidelity best effort. There is no post-generation exact overlay, no post-reconstruction visual repair or comparison, and no V4/V5 runtime fallback.
