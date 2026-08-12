# Editable PPT Workflow 2.1.0 快速开始

下载不可变的 `v2.1.0` Windows Release ZIP：

`https://github.com/czd176224-rgb/editable-ppt-workflow/releases/download/v2.1.0/editable-ppt-workflow-2.1.0-windows.zip`

同时下载 `SHA256SUMS.txt`，用 PowerShell `Get-FileHash` 核验后解压并运行 `install.ps1`，再重启 Codex。

上传一份已分页的 `.docx` 和一份 `.svg` Logo。只需完成一次最终 UI 提交：确认整体风格、每页正文、附件提取结果、具体生图要求，并对每张候选参考图明确保留或移除。

没有确认参考图时使用 Image2 `generate`；有 1–16 张确认参考图时使用 `edit`。随后执行轻量 QA、对象级可编辑重建，最后添加固定标题、原始 SVG Logo、页脚和页码。
