# 常见故障（V6 adaptive）

## 最终 UI 无法提交

每张 staged reference 都必须 explicit keep/remove。检查所有页面的材料 JSON、参考图决定和修订号；最终提交是 sole material/reference authority，提交后后台不会静默修改。

## Image2 输出比例错误

输出必须是 1904x896，并符合 17:8 容差。错误尺寸会被 rejected rather than stretched or cropped；插件不会用拉伸或裁剪掩盖提供商错误。检查请求记录和候选状态后重试。

## 真实图片不像原图

有 `1–16 confirmed refs` 时会走 `edit`，但融合属于 high-fidelity best effort，never pixel-perfect。减少互相冲突的参考图、明确每张图用途，并确保使用的是最终 UI 中确认的原图。

## QA 服务不可用

QA outage 不阻断生成：插件回退 candidate1，并明确记录为 `unvalidated`。恢复服务后可新建项目重新生成，但不会伪造历史 QA 通过。

## 重建返回 `401 token_expired`

对象级重建使用独立 editppt authentication，可能与 Image2 登录状态不同。`401 token_expired` 表示该令牌已过期；重新完成 editppt/Codex 登录后重试重建。不要声称该状态下已完成在线重建。

## 固定标题、Logo、页脚或页码异常

这些内容都是 PPT 固定层：标题、original SVG logo、页脚、页码不应出现在 Image2 正文中。V6 不提供 V4/V5 runtime fallback、exact overlay 或 post-reconstruction visual repair。

## 安装后仍显示旧版本

完全退出并重启 Codex Desktop，再新建任务。运行 `verify.ps1` 查看插件版本和运行时诊断；不要手工修改个人 marketplace 配置。
