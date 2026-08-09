# 常见故障（V4）

## `await_style_confirmation`

完成当前 UI 的最终确认后重跑。该 UI 是临时版本化适配层；不要手工修改风格 JSON。

## Image2 生成失败或 `page_blocked`

确认 Codex 已登录、账户具备图片能力且网络可用。比例超出 17:8 的 1% 相对误差会触发修复或阻断。解决记录的认证、限流或输出问题后，再显式释放被阻断页面。

## `qa_backend_pending` / `reconstruction_backend_pending`

打开 Codex 桌面版或 CLI 并使用 ChatGPT 登录，确认本地 `codex app-server` 可启动。未登录、订阅能力不可用、超时、结构化输出无效、签名不一致或对象清单不合格时都保持 pending，不会使用通用模板或假设通过。解决原因后重复同一命令。

## `assembly_pending`

页面已完成但最终原子组装或机械 QA 未成功。检查记录的错误、关闭正在占用目标 PPTX 的 Office 程序，然后重跑。失败尝试不会发布半成品。

## 内容或图片不符合预期

检查对应 Word 页正文、批注和材料绑定。页内图片默认仅作参考；需要直接出现时，在本页批注中明确指定图片。风格要求应在 UI 合同中确认。

## 安装后找不到插件

完全退出并重启 Codex Desktop，再新建任务。运行 `.\verify.ps1` 检查 V4 元数据与运行时。

## 卸载

运行 `uninstall.cmd` 并按提示确认。卸载器不删除用户项目。
