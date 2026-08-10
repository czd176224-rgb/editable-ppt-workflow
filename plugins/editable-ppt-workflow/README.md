# Editable PPT Workflow 2.0.0

`word-ppt-workflow-v6` converts one paginated Word document and one SVG Logo into an object-level editable 16:9 PowerPoint.

V6 uses a fixed 1904x896 (17:8) body region. Every body candidate is a fresh `gpt-image-2 generate` call; Word images, attachments and searched materials are references and never become `edit` inputs. Page comments are authoritative and may modify Word facts. Inaccessible references and ineffective later candidates do not block production.

After light QA, each accepted body is reconstructed into editable objects. The plugin then adds the native title, original SVG Logo, footer and page number and assembles pages in Word order. Final validation is mechanical; OfficeCLI inspection is optional.

Use the `run-word-to-ppt-workflow` skill as the production orchestrator. V4/V5 commands are not exposed by the production entry.
