# 常见故障

## 双击 setup.cmd 后立即关闭

从解压后的文件夹运行，不要在 ZIP 预览窗口中直接运行。也可以右键文件夹空白处打开 PowerShell，执行：

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\install.ps1
```

## 找不到 Codex CLI

先安装并打开 Codex Desktop，再重试安装。如果已经安装，完全退出 Codex 后重新打开一次，让桌面应用完成 CLI 环境准备。

## 找不到 Python

安装 Python 3.10 或更高版本，并在安装界面选择把 Python 加入 PATH。重新打开 PowerShell，运行：

```powershell
python --version
```

## 找不到 PowerPoint 或 LibreOffice

PowerPoint 和 LibreOffice 都不是基础结构校验的强制条件。若需要无标记 Word 的物理分页或最终回渲染证明，推荐安装 Microsoft Office，也可以安装 LibreOffice 作为后备；缺失时插件应给出非阻塞提示。

## Marketplace 下载失败

确认能够访问 GitHub，并检查公司网络、防火墙或代理设置。公开版安装不要求 GitHub 登录，也不要求安装 GitHub CLI。

## 安装完成但 @ 找不到插件

完全退出 Codex Desktop，重新打开并新建任务。不要继续使用安装之前已经打开的旧任务。

## 图片生成失败

确认 Codex 已登录、当前账户具有图片生成能力且网络正常。单页失败会进入自动重试；持续失败时保存终端错误信息再提交问题。

## 页面内容不理想

先检查 Word 的分页标记、当前页原文和页内附件是否正确。风格需要调整时，应重新开始任务并在一屏实时网页中修改风格要求。插件不会把旧项目的风格或内容自动带入新项目。

## 卸载

双击 `uninstall.cmd`，输入 `YES`。卸载器只删除插件登记、公开 Marketplace 登记和带有所有权标记的插件隔离运行时；不会搜索或删除用户项目。
