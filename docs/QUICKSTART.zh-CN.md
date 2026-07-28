# 五分钟快速开始

## 安装前准备

准备一台 Windows 10/11 x64 电脑，并确认：

- 已安装并登录 Codex Desktop；
- 已安装 Python 3.10 或更高版本，并能在 PowerShell 中运行 `python --version`；
- 推荐安装 Microsoft PowerPoint；LibreOffice 只作为可选后备；
- 首次安装和生成页面时网络可用。

## 安装

1. 打开项目的 GitHub Releases 页面。
2. 下载名称包含 `windows.zip` 的最新安装包。
3. 解压 ZIP；不要直接在压缩包预览窗口中运行。
4. 双击 `setup.cmd`。
5. 等到窗口显示 `Installation complete`。
6. 完全退出并重新打开 Codex Desktop，然后新建任务。

## 第一次使用

1. 把一个分页 `.docx` 拖入新任务。
2. 输入 `@`，选择 `editable-ppt-workflow`。
3. 发送：

```text
请把我上传的分页 Word 转换为可编辑 PPT。
```

4. 浏览器会打开一屏实时风格选择。切换方向、颜色、字体和密度时可立即看到真实 Word 页的近似效果；只需确认一次。
5. UI 预览不调用 Image2，确认截图只用于项目审计，不参与页面生图。
6. 等待页面独立生成、检查、重建并返回最终 `.pptx`。

Word 中如果有连续的“第1页、第2页……”标记，插件优先按标记分页；完全没有标记时，才按 Word 或 LibreOffice 的实际物理分页处理。

## 没看到插件

确认 `setup.cmd` 成功后，重启 Codex 并新建任务。旧任务不会自动重新加载刚安装的插件。
