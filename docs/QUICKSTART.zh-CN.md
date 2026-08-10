# Editable PPT Workflow 2.0.0 快速开始

下载不可变的 `v2.0.0` Windows Release ZIP：

`https://github.com/czd176224-rgb/editable-ppt-workflow/releases/download/v2.0.0/editable-ppt-workflow-2.0.0-windows.zip`

同时下载 `SHA256SUMS.txt`，用 PowerShell `Get-FileHash` 核验后解压并运行 `install.ps1`，再重启 Codex。

上传一份已分页的 `.docx` 和一份 `.svg` Logo，调用“分页 Word 独立生成可编辑 PPT”。UI 只确认一次全局视觉风格。V6 将逐页使用 Image2 `generate` 生成 17:8 正文、轻量 QA、对象级可编辑重建、加入固定标题/Logo/页脚/页码，并按 Word 顺序组装。

附件或搜索材料不可用不会阻断；参考材料不会触发 Image2 `edit`。OfficeCLI 检查是可选后处理。
