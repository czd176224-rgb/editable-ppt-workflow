# Editable PPT Workflow

将一个已经分页的 Word 文档转换为风格统一、逐页生成并可编辑重建的 PowerPoint。插件面向 Windows 版 Codex Desktop/CLI，工作流合同为 `word-only-v1`。

## 能做什么

- 只要求上传一个 `.docx` 文件。
- 优先识别文档中的“第1页、第2页……”标记；完全没有标记时使用 Word 或 LibreOffice 的物理分页。
- 锁定一页 Word 对应一页 PPT，不跨页合并。
- 通过一次连续的三阶段网页交互确认整套演示文稿的视觉方向。
- 根据锁定后的共同风格，独立生成、检查、修复和重建每一页。
- 最后按原页序机械组装，并检查 PPT 文件是否可打开和回渲染。

## 真实限制

- 目前只支持 Windows 10/11 x64。
- 首次安装需要网络，并需要 Python 3.10 或更高版本。
- 物理分页和回渲染需要 Microsoft PowerPoint 或免费的 LibreOffice；安装包不包含 Microsoft PowerPoint。
- 图片生成依赖 Codex 账户可用的图片生成能力。复杂页面可能耗时较长，也可能需要自动重试。
- 可编辑重建会尽力恢复文本和主要对象，但无法保证与生成图片达到像素级一致。
- 原文越密集，字号越可能减小；插件会优先保持内容和逻辑对应关系。

## 三步开始

1. 从 [Releases](https://github.com/czd176224-rgb/editable-ppt-workflow/releases/latest) 下载 Windows ZIP 并解压。
2. 双击 `setup.cmd`，等待环境检查和安装完成。
3. 重启 Codex，新建任务，上传分页 Word，输入 `@editable-ppt-workflow`。

复制下面这句话即可：

```text
@editable-ppt-workflow 请把我上传的分页 Word 转换为可编辑 PPT。
```

完整的新手说明见 [快速开始](docs/QUICKSTART.zh-CN.md)，日常操作见 [使用说明](docs/USER_GUIDE.zh-CN.md)，安装失败见 [常见故障](docs/TROUBLESHOOTING.zh-CN.md)。

## 命令行安装

如果已经克隆本仓库：

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\install.ps1
```

更新：

```powershell
.\update.ps1
```

卸载插件但保留隔离运行时：

```powershell
.\uninstall.ps1
```

完整卸载插件、公开 Marketplace 和插件自有运行时：

```powershell
.\uninstall.ps1 -RemoveRuntime -RemoveMarketplace
```

卸载器不会搜索或删除用户创建的 Word、PPT 或项目目录。

## 插件包含的 Skills

- `word-to-editable-ppt`
- `codex-gpt-image`
- `image-to-editable-ppt`
- `officecli`

这些 Skills 随插件一起安装，不要求用户逐个寻找。安装完成后请新建 Codex 任务，让 Codex 重新载入插件清单。

## 隐私与联网

Word 内容、页面提示和当前页所需的图片可能被发送到已配置的图片生成或 OCR 服务。项目缓存默认保存在当前项目目录，不会作为插件的跨项目记忆。请勿处理超出组织政策允许范围的机密材料。详情见 [安全说明](SECURITY.md)。

## 开发与验证

```powershell
python -m pip install -r plugins\editable-ppt-workflow\skills\word-to-editable-ppt\requirements-dev.txt
python -m pytest plugins\editable-ppt-workflow\skills\word-to-editable-ppt\tests -q
python -m pytest plugins\editable-ppt-workflow\skills\codex-gpt-image\tests -q
python -m pytest plugins\editable-ppt-workflow\skills\image-to-editable-ppt\cli\tests -q
python -m pytest tests -q
```

## License

[MIT](LICENSE)
