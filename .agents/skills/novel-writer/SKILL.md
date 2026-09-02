---
name: novel-writer
description: 按持久化剧情圣经、风格规则和章节模板规划、生成并审查长篇小说章节；用于小说自动创作、章节续写、剧情状态推进和伏笔管理。
---

# Novel Writer

## 必须遵守的工作流

每次生成章节前按顺序读取本 Skill 以及 [story-bible.md](references/story-bible.md)、[style-rules.md](references/style-rules.md)、[chapter-template.md](references/chapter-template.md) 和 [review-rubric.md](references/review-rubric.md)。Story Bible 是剧情事实唯一来源；与用户大纲或当前状态冲突时停止生成，输出结构化冲突报告。

只加载本章相关人物、地点、事件、未回收伏笔和最近章节摘要，不把完整长篇大纲无上限塞进提示词。先生成 beat sheet，再生成结构化 JSON 章节。章节完成后先保存 `proposed-story-state`，经过独立一致性审查后才更新剧情状态；不得直接覆盖大纲。

## 创作硬规则

开头尽快出现事件或冲突；本章有可描述的目标；主角采取行动；至少发生一次信息、关系或局势变化；结尾有合理悬念或新问题；行为符合动机；避免重复和连续水文。不得模仿具体作者/小说，不复制已有作品的段落、人物、情节组合或受版权保护表达。严格遵守用户世界观和大纲。

## 输出契约

只接受可解析的 JSON 对象，字段必须符合 [chapter-template.md](references/chapter-template.md)。非法 JSON 先做安全修复，失败后仅重试一次；仍失败则任务 `FAILED`，不发布不完整内容。`warnings` 不能掩盖 `blockingIssues`。

## 状态与安全

章节状态为 `PENDING`, `PLANNING`, `GENERATING`, `REVIEWING`, `DRAFT_READY`, `WAITING_APPROVAL`, `PUBLISHING`, `PUBLISHED`, `FAILED`, `CANCELLED`。审查有阻断问题不得发布；默认需要人工批准。API key 只由服务端环境变量读取。

详细事实、风格、模板和审查门槛见 references；修改用户剧情事实时必须先产生新版本并保留旧版本。
