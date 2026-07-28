# Word to Editable PPT

`word-only-v1` converts one paginated `.docx` into one editable PPT slide per Word page.

Flow:

`Word pagination lock → one embedded three-stage browser confirmation phase → immutable shared style contract → independent images/generations page work → relaxed page-local QA and targeted repair → independent editable reconstruction → locked-order mechanical assembly`

The browser component is embedded and adapted from the PPT Master interaction model; no separate Skill, package, or filesystem path is required.

```powershell
python scripts\word_to_editable_ppt.py prepare --word D:\Input\source.docx --output D:\Projects\Deck
python scripts\word_to_editable_ppt.py workflow next --project D:\Projects\Deck
python scripts\word_to_editable_ppt.py confirm-ui start --project D:\Projects\Deck
python scripts\word_to_editable_ppt.py confirm-ui wait --project D:\Projects\Deck --stage final
python scripts\word_to_editable_ppt.py confirm-ui shutdown --project D:\Projects\Deck
```

Ordered `第N页` markers take priority. Physical Word/LibreOffice pagination is used only when the document contains no markers. Invalid marker sequences stop the run. Page count, order, source text, and hashes remain locked.

The final browser result compiles deterministically into one immutable `style_execution.json`. Every initial page uses the exact same style bytes and `images/generations`. `images/edits` is reserved for local repair of an existing current-page image; structural, logic, or material overall-style repairs regenerate the page.

Stage 2 freezes the selected visual direction, six-role palette, CJK/Latin fonts, four-level type scale, information density, icon/image language, formal/modern/minimal degrees, and natural-language additions. Stage 3 freezes image quality, concurrency, automatic local-repair budget, editable output, and start mechanics. The selected `ppt169` or `ppt43` canvas maps to a legal equal-ratio `gpt-image-2` size and the same final PowerPoint slide ratio, with no crop.

QA is page-local and qualitative: overall style match plus preservation of the current page's main content, key facts, and main logic. Valid summarization, rewording, reordering, layout variation, and small visual differences are allowed. Pages proceed and reconstruct independently, including while other pages repair.

Final assembly waits for all locked pages and checks only page count/order, artifact integrity, page-QA status, editable objects, package validity, and open/back-render capability. Public releases are installed through the `editable-ppt-public` Marketplace; development changes are not visible until a new release is published.

Runtime outputs use `06_images/generated/` for page images and `08_final/` for the final PPTX, mechanical QA report, and run summary.
