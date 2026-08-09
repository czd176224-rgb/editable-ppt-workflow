# 五分钟快速开始（V5）

## 准备

- Windows 10/11 x64、Python 3.10+、已登录 Codex Desktop；
- 一个已经分页的 `.docx` 和一个原始 `.svg` 企业 Logo；
- Codex 账户具备 Image2 能力；
- 在 Codex 桌面版或 CLI 中使用 ChatGPT 登录；QA 和视觉重建由 Codex App Server 使用订阅能力完成，无需 API key；
- PowerPoint 推荐，LibreOffice 仅作可选分页/渲染后备。

## 安装

下载不可变的 `v1.1.0` Windows Release ZIP，校验发布校验和后解压运行：

```powershell
$Version = "1.1.0"
$Asset = "editable-ppt-workflow-$Version-windows.zip"
$ZipUrl = "https://github.com/czd176224-rgb/editable-ppt-workflow/releases/download/v1.1.0/editable-ppt-workflow-1.1.0-windows.zip"
$Base = "https://github.com/czd176224-rgb/editable-ppt-workflow/releases/download/v1.1.0"
Invoke-WebRequest $ZipUrl -OutFile $Asset
Invoke-WebRequest "$Base/SHA256SUMS.txt" -OutFile SHA256SUMS.txt
$Expected = ((Get-Content SHA256SUMS.txt | Where-Object { $_ -match [regex]::Escape($Asset) }) -split "\s+")[0].ToLowerInvariant()
$Actual = (Get-FileHash -Algorithm SHA256 $Asset).Hash.ToLowerInvariant()
if (-not $Expected -or $Actual -ne $Expected) { throw "Release ZIP SHA-256 verification failed." }
$InstallDir = Join-Path $PWD "editable-ppt-workflow-$Version"
if (Test-Path -LiteralPath $InstallDir) { throw "Install directory already exists: $InstallDir" }
Expand-Archive -LiteralPath $Asset -DestinationPath $InstallDir
& (Join-Path $InstallDir "setup.cmd")
```

安装后完全重启 Codex Desktop 并新建任务。

## 第一次运行

上传 Word 与 SVG Logo，选择 `editable-ppt-workflow`，要求转换为可编辑 PPT。UI 只打开一次全局风格确认；页面批注摘要只读，UI 审计图不会进入 Image2。

可直接在 Word 分页批注中写“文字表达图片化”“本页使用新闻稿图片”“采用水墨插画”。内部方括号指令仍兼容，但普通用户无需使用。分页批注优先于 UI 的软风格；Word 事实、表格、固定 1904×896 正文几何和 SVG Logo 始终不可覆盖。需要外部图片时插件会自动搜索并记录来源；必需素材找不到时返回 `material_blocked`，不会进入 Image2。

插件通过 Codex App Server 和 ChatGPT OAuth 使用订阅能力，不需要 `OPENAI_API_KEY`，也不产生 OpenAI API Key 路径的单独调用账单；任务仍受所用 ChatGPT/Codex 订阅的模型、图片和速率额度限制。

之后每页都生成精确 1904×896 的 17:8 正文设计。已接受的 Image2 设计是重建的视觉权威：重建只能恢复可编辑对象，不得自行简化或重新设计。最终通过“Image2 原图—可编辑页”成对 QA 后，再加入固定标题、Logo、页脚和页码；最终 PPT 为 16:9。

若返回 `await_style_confirmation`、`comment_resolution_pending`、`material_blocked`、`qa_backend_pending`、`reconstruction_backend_pending` 或 `assembly_pending`，按提示补齐确认、材料、登录或服务条件，然后重复同一命令/任务即可安全续跑。
