# 小说自动创作智能体数据模型

## 存储与版本

当前仓库没有已有数据库，基线采用 SQLite。所有表使用 UUID/text 主键、UTC ISO 时间戳、追加式 migration；迁移不得删除或重命名已有列。JSON 字段使用受 schema 校验的 JSON 文本，正文与原始模型响应分开保存。

## 实体

| 实体 | 关键字段与约束 |
| --- | --- |
| Novel | `id`, `title`, `genre`, `current_chapter_number`, `story_status`, `skill_version`, `story_bible_version`, `paused_at`, timestamps |
| StoryBible | `novel_id UNIQUE`, 当前版本指针与摘要；完整事实分拆为关联实体 |
| StoryBibleVersion | `novel_id`, `version`, `content_json`, `source`, `created_by`; `UNIQUE(novel_id, version)` |
| Character | `novel_id`, `key`, `name`, `role`, `motivation`, `traits`, `status`; `UNIQUE(novel_id,key)` |
| WorldRule | `novel_id`, `key`, `description`, `severity`; `UNIQUE(novel_id,key)` |
| TimelineEvent | `novel_id`, `event_key`, `occurred_at`, `description`, `chapter_number`; 唯一事件键 |
| Foreshadowing | `novel_id`, `key`, `description`, `status`, `introduced_chapter`, `resolved_chapter`; 状态为 `OPEN/RESOLVED` |
| Chapter | `novel_id`, `chapter_number`, `status`, `title`, `goal`, `content`, summaries, `published_at`; status includes `EXPORTED/PUBLISHED_MANUALLY`; `UNIQUE(novel_id,chapter_number)` |
| ChapterDraft | `chapter_id`, `version`, structured output, proposed story state, raw response, review pointer; 追加版本 |
| GenerationJob | `novel_id`, `chapter_id`, `status`, `idempotency_key`, attempts, lease, error; 活动任务唯一 |
| ReviewResult | `chapter_draft_id`, passed, score, issues, warnings, blocking issues, checked_at |
| PublishJob | `chapter_id`, publisher, status, idempotency key, external id, attempts, error; 发布幂等唯一 |
| GenerationUsage | `generation_job_id`, model, prompt version, input/output tokens, duration, request status |

## 状态机

章节任务允许：`PENDING -> PLANNING -> GENERATING -> REVIEWING -> DRAFT_READY -> WAITING_APPROVAL -> PUBLISHING -> PUBLISHED`。任何可恢复异常可进入 `FAILED`；用户取消进入 `CANCELLED`。`PUBLISHED` 和 `CANCELLED` 为终态。仅 `DRAFT_READY/WAITING_APPROVAL` 可进入发布前检查，且 `ReviewResult.blocking_issues` 必须为空。

## 事务边界

1. 创建 Novel 与初始 StoryBibleVersion 在一个事务中完成。
2. 创建生成任务使用章节唯一键和幂等键；重复请求返回已有任务。
3. 生成响应先写 `ChapterDraft`，Reviewer 写 `ReviewResult`。
4. Reviewer 通过后，在一个事务中写入 `proposed_story_state` 并更新关联 StoryBible/Novel 进度；失败回滚，不覆盖原状态。
5. 发布成功确认、Chapter 状态、PublishJob 和下一章任务必须在一个事务中更新。

## 安全与保留

API key 只存在进程环境变量，永不进表。原始响应可按部署保留期限清理，但结构化章节、审查结果、用量和审计字段必须可追溯。用户输入须按 novel 所属身份授权；错误对外只返回稳定 `code/message/details`。
