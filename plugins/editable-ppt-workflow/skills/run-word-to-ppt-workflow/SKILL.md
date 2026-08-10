---
name: run-word-to-ppt-workflow
description: Convert one paginated Word document and one SVG Logo into a resumable, every-page Image2, object-level editable 16:9 PowerPoint with the V6 generate-only workflow.
---

# Run Word-to-PPT Workflow V6

This is the only production workflow. Its authoritative state is `workflow_v6.json`. Do not invoke V4/V5 commands or migrate their state. Start a fresh V6 project from the user's Word and SVG Logo.

## Fixed architecture

The deck is 16:9. The body is 1904x896 (17:8) at `x=0.81, y=2.3, w=23.78, h=11.18 cm`. Title, Logo, footer and page number are fixed native PPT layers added after body reconstruction.

For every Word page:

1. Lock the complete page Word content, comments, Word images and attachment links.
2. Confirm one global visual-style contract in the UI.
3. Apply page comments as authoritative instructions; comments may modify Word facts.
4. Resolve only searches requested by comments. Public search records contain only material purpose and page number.
5. Treat Word images, accessible attachments and correct searched materials as references. If unavailable or invalid, record the status and continue without them—even if a comment requested them.
6. Call Image2 with `generate` only. Reference presence never selects `edit`; no V6 body request has an image input.
7. Light QA checks only 17:8 geometry, effective Word/comment requirements, global style, readability/relevance and absence of actively generated fixed layers. It does not require exhaustive facts, exact material reproduction, reconstruction suitability or later visual comparison.
8. Retry within the candidate budget. If QA is unavailable, later generation fails or candidates do not improve, use the first valid candidate.
9. Reconstruct the accepted body as object-level editable PPT content, add exactly four fixed layers, then assemble pages in Word order.
10. Perform mechanical OpenXML, slide-count, fixed-layer and editable-object validation. OfficeCLI is optional and post-production only.

The workflow never pauses for inaccessible attachments, failed searches or ineffective retries. A real provider/authentication failure before the first candidate remains a technical failure that can be retried.

## V6 commands

```powershell
python scripts\word_to_editable_ppt.py v6 init --word D:\Input\source.docx --logo D:\Input\logo.svg --project D:\Projects\Deck
python scripts\word_to_editable_ppt.py confirm-ui start --project D:\Projects\Deck
python scripts\word_to_editable_ppt.py v6 confirm-style --project D:\Projects\Deck --ui-result D:\Projects\Deck\confirm_ui\result.json
python scripts\word_to_editable_ppt.py v6 generate-page --project D:\Projects\Deck --page 1
python scripts\word_to_editable_ppt.py v6 reconstruction-request --project D:\Projects\Deck --page 1
python scripts\word_to_editable_ppt.py v6 finalize-page --project D:\Projects\Deck --page 1 --body-pptx D:\Projects\Deck\reconstructed\page_001.pptx
python scripts\word_to_editable_ppt.py v6 assemble --project D:\Projects\Deck
```

The outer Codex skill owns the reconstruction handoff: use `reconstruct-editable-slide` for each request, then immediately finalize the page and continue until assembly. Do not require the user to say “continue” between internal stages.

Use `doctor` for auth, CLI, DNS fake-IP, font and optional Office diagnostics. Never print tokens or put user documents/generated decks into the release package.
