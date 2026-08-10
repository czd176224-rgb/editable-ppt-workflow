# Editable PPT Workflow 1.2.0

本插件的生产合同是 `word-ppt-workflow-v5`：分页 Word 与 SVG Logo 经一次全局风格确认后，逐页生成 Image2 视觉设计，按设计高保真重建为可编辑对象，完成固定图层后执行成对视觉 QA、顺序组装和强制 Office 验证。

幻灯片固定 16:9，正文区域固定为 `x=0.81、y=2.3、w=23.78、h=11.18 cm`。每个页面都调用 Image2 生成完整正文设计；目标宽高比为 17:8，相对误差不超过 1%。超限、事实不符、批注未满足、必需图片缺失或可读性不合格时，只允许有界定向修复，未解决页面绝不组装。

Word 页内图片默认是 Image2 参考素材，是否直接出现由精确页内批注或当前 UI 归一化风格合同决定。附件与按批注搜索的材料均按本页不可信证据处理并记录来源。Logo 永不进入 Image2。

已接受的 Image2 正文图是视觉布局权威；Word 正文与表格是事实权威。视觉重建按原稿的构图、几何、层级、配色、留白与装饰输出对象清单，本地后端重建并重新打开 PPTX，验证原生文字、原生表格、局部图片来源、对象可见性以及不存在整块正文栅格。随后加入恰好一个标题、原始 SVG Logo、页脚和页码，并把最终页与 Image2 原稿成对审查。

分页自然语言批注先解析为页面要求。优先级为硬规则、Word 事实/表格、分页批注、UI 全局软风格、证据、模型创意；批注不能覆盖事实或固定图层。需要新闻稿图片等外部素材时会自动搜索并记录来源，必需材料缺失则在 Image2 前进入 `material_blocked`。内部方括号指令仍兼容但不是普通用户的必需写法。

确认 UI 每个项目只出现一次，页面要求摘要只读。缓存按材料、生成、QA、重建、完成页和最终组装分层绑定；Word、Logo、页面批注或确认风格变化不会被旧缓存覆盖。

```powershell
python skills\run-word-to-ppt-workflow\scripts\word_to_editable_ppt.py run `
  --word D:\Input\source.docx --logo D:\Input\logo.svg --output D:\Projects\Deck --wait-ui
```

缺少风格确认、Codex/ChatGPT 登录、订阅额度或本地运行服务超时时，会返回可恢复的 pending 状态。批注解析、搜索、QA 与可编辑重建通过 Codex App Server 运行，由 Codex 管理 OAuth；生图使用同一 ChatGPT 订阅下的 Codex OAuth 图片能力并保持精确像素尺寸。插件不需要、不会调用 `OPENAI_API_KEY`，也不安装 OpenAI Python SDK；订阅模型、图片和速率额度仍适用。最终组装在本地完成，重复运行同一命令会验证阶段产物后续跑。

`run` 在一次确认后只负责建立或恢复 V5 DAG，并把当前可执行节点交给本次 Codex Skill。逐页 Image2、QA 与重建由 Skill 继续调度；Python 命令本身不会生成 Codex 子代理，也不会再进入旧 V4 QA、重建或装配链。
