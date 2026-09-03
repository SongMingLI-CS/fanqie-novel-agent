# 小说自动创作智能体进度

## 当前状态

- 分支：`codex/novel-agent`
- 开始日期：2026-09-02
- 当前阶段：Phase 2-10 基础实现完成
- 最近提交：f9447bc

## Phase 1：审计、Skill 和剧情状态模型

状态：完成。

已完成：

- 检查 Git 状态：空仓库、无未提交用户文件、无历史提交。
- 检查项目入口：不存在框架、包管理器、数据库、部署平台、schema、API 或既有 Agent/Job/Canvas 抽象。
- 建立独立分支 `codex/novel-agent`。
- 创建小说创作 Skill 及四份参考文件。
- 记录服务端/Worker/SQLite 基线决策、章节状态机、数据实体和持久化不变量。

验证：

- `git diff --check`：待本阶段提交前运行。
- 自动化测试：仓库尚无测试运行器，安排在后续 Phase 建立。

风险/未完成：

- 运行时尚未建立；DeepSeek 凭据、外部平台授权和部署环境均待配置。
- SQLite、Fastify、TypeScript 是针对空仓库的明确基线决策，不是对现有项目的审计发现。

## Phase 2-10：可运行基线

状态：基础闭环完成，后续增强项保留。

- 服务端 DeepSeek client：服务端环境变量、可配置模型/地址/超时/token、有限指数退避、用量记录、非法 JSON 一次重试。
- 规划/生成/审查：读取四份 Skill reference，裁剪最近摘要，保存结构化章节、审查结果和 proposed state。
- SQLite 与 Worker：章节唯一键、任务幂等、失败恢复、取消、暂停标志、独立 Worker 进程。
- 半自动发布：TXT/Markdown/JSON 导出和人工发布确认；阻断审查不可导出；不接入番茄网页。
- API/控制台：小说、StoryBible、章节、job、生成、审查、批准、导出、人工发布、暂停/继续路由及最小控制页。
- 测试：8 项单元/集成级测试覆盖 StoryBible、结构化生成、非法响应、重复/恢复、审查阻断、导出和人工发布。

验证命令与结果：

- `python3 -m unittest discover -s tests -v`：8/8 通过。
- `python3 -m compileall -q novel_agent tests`：通过。
- `git diff --check`：通过。

限制与下一步：

- 本仓库没有既有 lint/typecheck/build 工具，Makefile 以 Python 编译检查和 diff 检查作为可重复基线；未伪造 TypeScript 或前端 production build 结果。
- DOCX 不实现（审计发现无既有文档能力）；可未来增加明确适配器。
- `NOVEL_AUTH_TOKEN` 已提供 Bearer 身份验证开关；未配置时仅适用于受保护的本机开发环境。
- Worker 已按 `NOVEL_WORKER_CONCURRENCY` 使用独立连接并以 SQLite `BEGIN IMMEDIATE` 抢锁；租约、超时恢复和 `NOVEL_JOB_TIMEOUT` 已实现，横向多实例仍需生产数据库/队列增强。
- 控制台已覆盖小说、章节、生成和暂停/继续的最小操作；富文本编辑、逐字段 StoryBible 编辑和更完整的审核视图仍是后续增强项。

状态门禁修正：

- 导出现在同时要求 `ReviewResult.passed=true`、无 `blockingIssues` 且章节处于 `DRAFT_READY/WAITING_APPROVAL`。
- 人工发布现在严格要求先有 `EXPORTED` 状态，并在确认事务中推进 `current_chapter`。
- 重复导出返回既有文件和 `idempotent=true`；无效状态不会覆盖已有稿件。
- 暂停小说拒绝新生成任务；`continue` 只创建当前下一章，连续 N 章也必须逐章人工确认后继续。
- 修改草稿会清空旧审查结果并回到 `REVIEWING`，必须重新审查后才能导出。
- Worker 对网络/服务错误按最大次数重试并进入 `FAILED`；非法 JSON 的修复重试失败会直接进入死信，不重复发布。
- 控制台已增加 StoryBible 编辑、草稿修改、重新审查、导出和人工发布确认操作。
- 章节结构现在独立保存 `summary` 与 `beats`，部署/备份/恢复说明见 `docs/novel-agent-deployment.md`。
- 新增追加式 `chapter_drafts`、`review_results`、`publish_records`，并提供小说级用量查询，保留编辑和人工发布历史。
- Reviewer 现在阻断非对象响应、未授权世界规则、时间线事件重定义和未开放伏笔回收；过期运行任务可在 Worker 重启后被重新领取。
- API 增加章节详情、`/publish` 人工确认兼容路由和小说级用量查询；所有未知 API 路由/资源返回结构化 404。
- 人工发布确认事务现在会应用已审查的 `proposed-story-state`，创建新 StoryBible 版本并更新事件、伏笔和当前剧情位置。
- 新增持久化 `export_jobs` / `publish_jobs`，导出幂等键和人工发布状态不再只依赖文件或 Chapter 投影。
- 补齐安全发布适配器抽象：DryRun、LocalFile 和无默认实现的 ExternalPublisher 契约。
- 发布适配器对缺省元数据安全降级，避免导出接口因可选摘要字段缺失而异常。
- DeepSeek Base URL 不再硬编码；未注入地址或 API Key 时显式安全失败。
- DeepSeek 失败请求现在也写入 `GenerationUsage`，保留模型、Prompt 版本、失败状态和安全错误信息。
- Job 失败重试现在持久化 `next_attempt_at`，使用有限指数退避，服务重启不会立即重复请求。
- 控制台展示结构化审查结果以及模型、输入/输出 Token、耗时和请求状态。
- DeepSeek 上下文现在只选取当前章节所需事实并限制在 14,000 字符以内；Reviewer 使用 StoryBible 的 `styleRules.chapterLength` 做长度门禁。
- 新增真实 HTTP 集成测试，启动项目服务端验证小说创建、章节任务创建和重复请求幂等。
- Job 取消现在与 Chapter 状态在同一事务中同步为 `CANCELLED`，重复取消返回无变更。

## 2026-09-03：DeepSeek V4 真实调用排障

状态：完成，原 Job 已真实恢复并生成第 1 章待审草稿。

- 当前真实 Novel `75747e6b-1c90-4d47-b39a-a4e856f8cd62` 的 StoryBible 保持版本 1；第 1 章正文、草稿、导出和发布记录均为空。
- 当前真实 Job `4cb25ce0-b033-44a1-a08c-c1de8efbe7ae` 唯一存在，可原地恢复；没有重复幂等键。
- 最小认证请求在 Worker 相同 Python/CA 环境中返回 HTTP 200，Base URL 为 `https://api.deepseek.com`，模型为 `deepseek-v4-flash`。
- 根因一：旧流式实现最终调用 `response.read()`，未逐事件消费 SSE，无法准确区分首字节、流读取与整体超时。
- 根因二：V4 Flash 默认 thinking；低输出预算可被 reasoning 消耗，导致 `message.content` 为空。结构化章节默认关闭 thinking，并保留环境变量开关。
- 客户端现已逐行缓冲 SSE；仅收到 `[DONE]` 后返回完整 `message.content`。中断、首字节超时、流读取超时或整体超时时丢弃内存缓冲，不写正式 Chapter。
- 新增 DNS、TCP、TLS、HTTP 400/401/429、连接/首字节/流读取/整体超时、完整流、日志脱敏和失败不污染数据库测试。
- `.env`、`.env.local`、`.DS_Store` 和运行数据库均排除在版本控制之外。
- 原 Job `4cb25ce0-b033-44a1-a08c-c1de8efbe7ae` 原地恢复成功，数据库中仍只有一个第 1 章生成 Job。
- 真实调用模型 `deepseek-v4-flash`，thinking 关闭，HTTP 200；耗时 23,421ms，输入 1,652 Token，输出 2,337 Token。
- 第 1 章《天道有Bug》保存为 `WAITING_APPROVAL`，正文 2,277 字符；自动审查通过，得分 100，无 blocking issue。
- StoryBible 仍为版本 1、Novel 当前正式章节仍为 0；候选剧情状态未正式提交。导出、发布 Job 和人工发布记录均为 0。

验证：`make lint && make typecheck && make test && make build`，40/40 测试通过。

## 2026-09-03：审查通过后自动导出 TXT 草稿

状态：完成。

- 增加 `NOVEL_AUTO_EXPORT_TXT`，默认开启；完整生成且自动审查通过后写入稳定命名的 TXT。
- 自动 TXT 使用临时文件加原子替换，进程中断不会留下半截正式文件。
- 自动草稿导出不创建可发布的 ExportJob，不改变 `WAITING_APPROVAL`，不更新 StoryBible，也不触发发布。
- 生成失败或审查阻断时不产生 TXT。
- 已将真实第 1 章导出到 `data/exports/天道开源_我靠多智能体集群把修仙界卷破产_卷一_边缘计算与游击战_第0001章_天道有Bug.txt`；章节仍为 `WAITING_APPROVAL`，StoryBible 仍为版本 1。
- 导出文件名不再使用 UUID，改为 `<小说名>_<卷名>_第0001章_<章节标题>.txt`，并清理跨平台不安全字符。
- `make lint && make typecheck && make test && make build`：41/41 测试通过。

追加验证：

- `make lint && make typecheck && make test && make build`：25/25 测试通过。
- 并发锁验证：Worker 使用 `BEGIN IMMEDIATE`，每个并发槽使用独立连接；未接入真实 DeepSeek，未伪造外部服务结果。
- HTTP 冒烟：`GET /` 与 `POST /api/novels` 通过；过程创建的临时 SQLite 已移出仓库。
