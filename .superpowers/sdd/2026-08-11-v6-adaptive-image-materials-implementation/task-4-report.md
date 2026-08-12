# Task 4 report: secure V6 reference media

Status: complete.

## RED and GREEN evidence

- RED: `python -m pytest tests/test_workflow_v6_media.py tests/test_confirm_ui_contract.py -q` produced 7 failures: the media module and authenticated media route did not exist.
- Additional RED checks proved the old lifecycle stored no thumbnail and that `model-input.jpg` was rejected by the variant resolver.
- GREEN: `python -m pytest tests/test_workflow_v6_media.py tests/test_confirm_ui_contract.py -q` passed `106 passed, 1 skipped` (the skip is Windows symlink capability).
- Focused V6 regression: all `test_workflow_v6_*.py` files passed `83 passed, 1 skipped`.
- `git diff --check` passed.

## Delivered files

- Added `scripts/workflow_v6_media.py`: decoded-content validation, encoded/pixel/edge limits, EXIF-safe photo and PNG screenshot derivatives, restricted SVG rasterization, and contained media resolution.
- Updated `scripts/workflow_v6_materials.py` and `scripts/workflow_v6_source.py`: both imported and Word-embedded references now retain original bytes and use bounded, hashed preview/model derivatives while preserving Task 3 states and metadata-only `source_url`.
- Updated `scripts/confirm_ui/server.py`: nonce-and-project-owner protected media route with `nosniff`, decoded safe MIME, attachment disposition, and no inline SVG/HTML serving.
- Added/updated media, UI contract, V6 source, and V6 lifecycle tests.

## Self-review

- Confirmed originals are copied byte-for-byte; raster model inputs/thumbnail paths resolve beneath `02_v6/reference_media` after symlink resolution.
- Confirmed 25 MB encoded, 80 MP decoded, and 16,384-edge limits are enforced; Pillow bomb warnings are errors.
- Confirmed SVG scripts, external resources, and unsafe elements are rejected before local minimal raster rendering.
- Confirmed the endpoint requires a valid existing lock owner and matching `X-Confirm-Nonce`; all outputs are decoded rasters with safe MIME and `X-Content-Type-Options: nosniff`.

## Concern

Windows does not grant test symlink creation in this environment, so the symlink-escape assertion is skipped locally. The resolver uses `Path.resolve(strict=True)` followed by `relative_to(project)` and is covered by the non-skipped traversal tests.

Commit: `afb035b` (`feat: secure v6 reference media`).

## Fix round 1: security hardening

Status: complete.

- RED: added tests exposed the original unsafe output-parent handling, page-local request-id collision, validation/read TOCTOU response mismatch, and toy SVG renderer blank output. A final adversarial `url(#local) url(https://...)` SVG paint test also failed before the sanitizer fix.
- GREEN: `python -m pytest tests/test_workflow_v6_media.py tests/test_confirm_ui_contract.py tests/test_workflow_v6_source.py tests/test_workflow_v6_cli.py -q` passed `133 passed, 1 skipped`; all `test_workflow_v6_*.py` passed `87 passed, 1 skipped`; `git diff --check` passed.
- Added safe no-overwrite media writes under link/reparse-checked parents, page-namespaced acquisition reference ids, one-buffer validated media reads, safe inline disposition for derivatives, and attachment originals.
- Replaced the limited SVG painter with a locally installed headless Chromium renderer using a fresh project-contained profile. The XML preflight rejects DTD/entity/script/event/external references and disallows external SVG resource elements; browser networking features are disabled. Realistic path/viewBox/gradient/transform/text SVG coverage passes.

Self-review: endpoint data is decoded from the same capped buffer that is returned; `lstat`/`fstat` plus no-follow handling rejects replacement and link swaps. The renderer fails explicitly if Chrome is unavailable or produces a blank/invalid result.

Concern: the controlled renderer depends on locally installed Chrome or Edge; absence is an explicit normalization failure rather than a degraded preview. The environment still lacks symlink privilege, so that one test skips; reparse/link guards are deterministically exercised by a platform-safe injected reparse check.

Commit: `9a6d38b` (`fix: harden v6 reference media`).

## Fix round 2: static SVG and handle-bound I/O

Status: complete.

- RED: tests demonstrated that `<set>` was not rejected, Chromium could overwrite a predictable project `.safe-render.png` hardlink, white logos were rejected as blank, and the handle-final-path guard was absent.
- GREEN: `python -m pytest tests/test_workflow_v6_media.py tests/test_confirm_ui_contract.py tests/test_workflow_v6_source.py tests/test_workflow_v6_cli.py -q` passed `139 passed, 1 skipped`; all `test_workflow_v6_*.py` passed `92 passed, 1 skipped`; `git diff --check` passed.
- SVG now uses a static element allowlist (including paths/text/gradients/clips/masks), rejects every other element plus animation and CSS attributes, and serializes only the parsed/sanitized tree. HTTP-probe coverage includes `feImage` plus `set attributeName=href`, CSS import, and external paint URLs with zero requests.
- Chromium now renders only in a new OS temporary directory and profile, always cleaned with a non-poisoning `finally`; project files are created exclusively through handle-verified `O_EXCL` writes. A hardlink regression proves predictable project render paths are untouched.
- Project media reads use one already-open handle, verify its final operating-system path is under the project root, cap bytes, decode that buffer, and return that same buffer. Injection coverage exercises ancestor-race escape rejection for both write and endpoint read paths.
- All-white logos are accepted after decode/dimension validation and a forced renderer failure leaves a retryable empty destination.

Self-review: static SVG validation occurs before any browser launch. Dynamic SVG tags and animation attributes cannot reach the renderer; browser input is the sanitized serialization. Final-path checks are intentionally injectable to exercise Windows ancestor-race behavior rather than relying on unavailable symlink privilege.

Concern: final-path verification relies on the operating system's handle-resolution API; on a platform lacking that API the implementation fails safely rather than falling back to a path reopen. Chromium remains a required local renderer dependency and temporary-directory cleanup is deliberately best-effort.

Commit: `aead8fb` (`fix: secure v6 svg media io`).

## Fix round 3: strict SVG grammar and stable-root handles

Status: complete.

- RED: `tests/test_workflow_v6_media.py` produced four expected failures for CSS-escaped `u\\72l(...)` paint, foreign/unknown SVG namespaces and attributes, stable root/file final-handle comparison, and pathname output hashing. Follow-on RED checks caught an unused foreign namespace declaration, source-side thumbnail pathname hashing, and endpoint pathname resolution before the one-handle read.
- GREEN: `python -m pytest tests/test_workflow_v6_media.py tests/test_confirm_ui_contract.py tests/test_workflow_v6_source.py tests/test_workflow_v6_cli.py -q` passed `145 passed, 1 skipped`; all `test_workflow_v6_*.py` passed `98 passed, 1 skipped`; `git diff --check` passed.
- `workflow_v6_media.py` now accepts only exact SVG-namespace elements and element-specific attributes, rejects foreign/xlink declarations, unknown attributes, style/event/dynamic content, CSS escapes/backslashes, and all paint/resource values except safe literals or exact local `url(#id)` references. The renderer still consumes only sanitized XML.
- Project media writes and reads now keep an open project-root handle and an open file handle through final-path comparison and payload I/O. Windows opens the root with `CreateFileW(... FILE_FLAG_BACKUP_SEMANTICS)`; POSIX uses a directory handle. No failed-verification pathname unlink occurs.
- Writer-returned SHA-256 digests are calculated from the exact bytes written, including the thumbnail digest now used by the acquisition lifecycle. Endpoint reads construct a syntactically constrained candidate then verify the live file handle rather than resolving/reopening an untrusted pathname.

Self-review: verified the endpoint's test replaces the file after decode and still receives the original validated buffer; root-swap and ancestor-swap tests reject before payload I/O; originals remain attachment downloads and safe derivatives remain inline. No Python URL fetch was introduced, and `source_url` remains metadata-only.

Concern: the local test environment still skips one symlink-capability test. Stable final-handle regression tests cover project-root and ancestor swaps without relying on that privilege. SVG rendering continues to require an installed local Chrome or Edge and fails explicitly if unavailable.

Commit: `fix: enforce v6 media stable handle containment`.
