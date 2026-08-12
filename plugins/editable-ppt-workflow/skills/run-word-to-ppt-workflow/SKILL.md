---
name: run-word-to-ppt-workflow
description: Use when converting one paginated Word document and one SVG Logo into a resumable, object-level editable 16:9 PowerPoint with confirmed per-page materials.
---

# Run Word-to-PPT Workflow V6

Use only `word-ppt-workflow-v6`. The original paginated Word and SVG Logo create a new V6 project; never migrate an old project or invoke a V4/V5 runtime fallback.

## Authoritative flow

1. `v6 init` locks the Word and original SVG Logo, resolves comments into effective body changes, attachment extraction requirements, and real-reference/image requirements.
2. Extract only necessary attachment text/table/chart facts. Inaccessible attachments are recorded and never block.
3. Each pending real-reference acquisition occurs once. Import a successful local result, then confirm or reject it; close an unavailable result as `failed_no_retry`. Never fetch it again automatically.
4. Open one Confirm UI interaction. Its final stage exposes editable per-page materials and safe thumbnail, original and model-input controls. The user may accept/reject found references and edit their purposes.
5. Submit once. The sealed result is the only prompt and QA authority. The backend never reinterprets comments, re-extracts materials, or changes confirmed facts.
6. Generate each body through `v6 generate-page`: zero valid confirmed references selects `generate`; one to sixteen valid confirmed references selects `edit`. Empty edit is invalid.
7. Retry at most once with the same operation and same original confirmed references, never candidate 1. Ordinary pages start at `medium`; Logo, screenshot, dense-data, small-text and high-detail risk pages start at `high`. Use at most two candidates, bounded concurrency and nonblocking first-candidate fallback.
8. Use `reconstruction-request`, hand the request to `reconstruct-editable-slide`, then `finalize-page`. Finally run `assemble`.

The 1904x896 (17:8) body excludes the fixed page title, original SVG Logo, footer and page number. They are added only as native fixed PPT layers. Logo, screenshot and real-photo fusion is high-fidelity best effort, not an exact-reproduction promise. There is no post-generation exact overlay and no post-reconstruction visual repair or comparison.

## Production commands

```powershell
python scripts\word_to_editable_ppt.py v6 init --word D:\Input\source.docx --logo D:\Input\logo.svg --project D:\Projects\Deck
python scripts\word_to_editable_ppt.py v6 import-reference --project D:\Projects\Deck --page 1 --request-id REF --image D:\Input\photo.jpg
python scripts\word_to_editable_ppt.py v6 confirm-reference --project D:\Projects\Deck --page 1 --reference-id REF
python scripts\word_to_editable_ppt.py v6 fail-reference --project D:\Projects\Deck --page 1 --request-id REF --reason unavailable
python scripts\word_to_editable_ppt.py confirm-ui start --project D:\Projects\Deck
python scripts\word_to_editable_ppt.py v6 confirm-style --project D:\Projects\Deck --ui-result D:\Projects\Deck\confirm_ui\result.json
python scripts\word_to_editable_ppt.py v6 generate-page --project D:\Projects\Deck --page 1
python scripts\word_to_editable_ppt.py v6 reconstruction-request --project D:\Projects\Deck --page 1
python scripts\word_to_editable_ppt.py v6 finalize-page --project D:\Projects\Deck --page 1 --body-pptx D:\Projects\Deck\reconstructed\page_001.pptx
python scripts\word_to_editable_ppt.py v6 assemble --project D:\Projects\Deck
```

Use `doctor` for authentication, CLI, DNS fake-IP, font and optional Office diagnostics. Never print tokens or package user inputs/outputs with the plugin.
