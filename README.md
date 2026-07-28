# Editable PPT Workflow

将一个已经分页的 Word 文档转换为逐页生成、逐页检查并可编辑重建的 PowerPoint。当前工作流合同为 `word-only-v1`。

## 使用方式

1. 安装后重启 Codex。
2. 新建任务并上传一个分页 `.docx`。
3. 输入：

```text
@editable-ppt-workflow 请把我上传的分页 Word 转换为可编辑 PPT。
```

插件优先识别 `第1页、第2页……` 标记；完全没有标记时才使用 Word 物理分页或 LibreOffice 后备。锁定后始终保持一页 Word 对应一页 PPT。

用户通过三个可返回的步骤填写视觉要求，但只进行一次最终确认。先选模板，再通过专业视觉化配置台调整细节，最后检查视觉、生产与交付合同；颜色支持 RGB/HEX 精确选择。局部视觉示意不调用 Image2，UI 截图只保存在项目中，不会投喂生图程序。生产模式提供质量优先、均衡和速度优先三档，并明确显示对应的图像质量、并发页数和自动修复次数。

## 能做什么

- 自动识别 Word 总页数、页序、正文、表格和页内图片/附件。
- 用统一的紧凑风格合同逐页独立调用 `gpt-image-2`。
- 在忠实本页信息和逻辑的基础上重组视觉表达。
- 只对异常页面做局部修图或重新生成。
- 页面通过 QA 后立即并行进入可编辑重建。
- 严格复用当前项目内未变化页面的缓存。
- 最终按锁定页序组装并检查页数、对象可编辑性和文件结构。

## 优点与代价

优点：用户输入少；只有一次人工确认；正常页面没有重复生图和重复深检；修改单页不会重做整套；页面构图仍由 Image2 根据内容独立决定。

代价：首次安装环境较大；图片生成和对象级重建仍需要时间；Image2 对密集文字并非绝对可靠；可编辑重建不能保证像素级一致；复杂或无法解析的 Word 附件可能需要本页额外处理。

## 安装

从 Releases 下载 Windows ZIP 后运行 `setup.cmd`，或克隆仓库后运行：

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\install.ps1
```

完整说明见 [快速开始](docs/QUICKSTART.zh-CN.md)、[使用说明](docs/USER_GUIDE.zh-CN.md) 和 [常见故障](docs/TROUBLESHOOTING.zh-CN.md)。

## 运行环境

- Windows 10/11 x64
- Python 3.10+
- Codex 登录及账户可用的图片生成能力
- Microsoft Word/PowerPoint 推荐但非强制
- LibreOffice 仅作为物理分页和回渲染后备

项目缓存默认仅存在当前项目目录，不会成为跨项目记忆。Word 页文本和必要的本页图片可能发送到 Codex Images；详情见 [安全说明](SECURITY.md)。

## 开发验证

```powershell
python -m pytest plugins\editable-ppt-workflow\skills\word-to-editable-ppt\tests -q
python -m pytest plugins\editable-ppt-workflow\skills\codex-gpt-image\tests -q
python -m pytest plugins\editable-ppt-workflow\skills\image-to-editable-ppt\cli\tests -q
python -m pytest tests -q
```

[MIT License](LICENSE)
