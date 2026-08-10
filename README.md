# Editable PPT Workflow 1.2.0

`word-ppt-workflow-v5` 将一个已分页 Word 和一个 SVG 企业 Logo 转为“一页 Word 对应一页 PPT”的高保真对象级可编辑演示文稿。

## V5 工作流

`锁定 Word 与 Logo → 编译页面意图与素材需求 → UI 一次全局确认 → 必需真实素材项目级搜索一次 → 每页 Image2 视觉设计 → 高保真对象级重建 → 添加固定图层 → Image2 原稿与最终页成对 QA → 按页序组装 → Office 验证`

- PPT 固定 16:9（25.4 × 14.288 cm）。
- 正文设计图目标 17:8，允许的相对宽高比误差不超过 1%；超限必须修复或阻断。
- 每个未命中有效缓存的页面都调用 Image2，不再按页面类型跳过。
- Word 页内图片默认只作为 Image2 素材；只有精确页内批注或已归一化全局风格要求才必须直接出现。
- 支持“文字表达图片化”“使用新闻稿图片”等自然语言批注；内部方括号指令仍兼容，但普通用户无需书写。
- 优先级固定为：硬规则、Word 事实/表格、分页批注、UI 全局软风格、证据材料、模型创意。批注只覆盖软风格，不能更改事实、几何或 Logo。
- 附件只作为本页不可信参考；批注明确需要新闻、照片或外部资料时会自动搜索并保留 URL、时间与本地文件哈希。必需材料缺失时进入 `material_blocked`，不会浪费 Image2 调用。
- 已接受的 Image2 正文图决定构图、层级、配色、留白和视觉节奏，Word 决定文字与表格事实。整块正文图片加隐藏文字不算可编辑重建。
- QA 在重建和固定图层完成后成对比较 Image2 原稿与最终可编辑页，同时检查原文、批注、真实素材、可读性和设计还原度；仅硬性问题阻塞，软建议不阻塞。
- 最终 PPT 必须通过 OpenXML 打开、Microsoft PowerPoint 或 LibreOffice 逐页渲染及对象可编辑性检查。
- 标题、原始 SVG Logo、页脚和页码在重建后分别加入固定区域，且每项恰好一个。

## 一条命令

```powershell
python plugins\editable-ppt-workflow\skills\run-word-to-ppt-workflow\scripts\word_to_editable_ppt.py run `
  --word D:\Input\source.docx --logo D:\Input\logo.svg --output D:\Projects\Deck --wait-ui
```

同一命令可安全续跑。全项目只出现一次整体风格确认，页面要求摘要为只读信息，不再逐页弹窗。未确认风格、缺少 Image2/Codex/ChatGPT 登录、本地 Codex App Server 超时或订阅额度暂不可用时会返回明确 pending 状态；补齐条件后重跑即可。

确认完成后，`run` 建立或恢复唯一的 V5 DAG，并返回当前 ready work；本次 `run-word-to-ppt-workflow` Codex Skill 随后调度 Image2、QA 和逐页重建子代理。Python 命令不会自行创建 Codex 子代理，也不会回落到旧 V4 生产链。

## 安装与验证

Windows 10/11、Python 3.10+、Codex 桌面版/CLI 的 ChatGPT 登录和图片生成能力为必需条件。视觉 QA 与视觉重建通过 Codex App Server 使用订阅额度；生图通过 Codex OAuth 图片能力保持固定像素尺寸；插件不使用 OpenAI API key。PowerPoint 推荐，LibreOffice 只作可选分页/渲染后备。

ChatGPT/Codex 订阅与 API 账单彼此独立：本插件只走 Codex 管理的 OAuth 和订阅能力，不读取 `OPENAI_API_KEY`，也不安装 OpenAI Python SDK；仍受用户订阅计划的图片、模型与速率额度约束。

公开安装固定到不可变标签 `v1.2.0`。推荐下载完整 Release ZIP 并核验 SHA-256：

```powershell
$Version = "1.2.0"
$Asset = "editable-ppt-workflow-$Version-windows.zip"
$ZipUrl = "https://github.com/czd176224-rgb/editable-ppt-workflow/releases/download/v1.2.0/editable-ppt-workflow-1.2.0-windows.zip"
$Base = "https://github.com/czd176224-rgb/editable-ppt-workflow/releases/download/v1.2.0"
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

卸载使用 `.\uninstall.ps1`。故障处理见 [快速开始](docs/QUICKSTART.zh-CN.md)、[使用说明](docs/USER_GUIDE.zh-CN.md) 与 [常见故障](docs/TROUBLESHOOTING.zh-CN.md)。

[MIT License](LICENSE)
