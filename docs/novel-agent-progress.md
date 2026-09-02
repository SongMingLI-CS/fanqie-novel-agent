# 小说自动创作智能体进度

## 当前状态

- 分支：`codex/novel-agent`
- 开始日期：2026-09-02
- 当前阶段：Phase 2-10 基础实现完成
- 最近提交：待提交

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
- 当前 Worker 是单进程单并发；租约、超时恢复和 `NOVEL_JOB_TIMEOUT` 已实现，横向多实例和更高并发仍需生产数据库/队列增强。
- 控制台已覆盖小说、章节、生成和暂停/继续的最小操作；富文本编辑、逐字段 StoryBible 编辑和更完整的审核视图仍是后续增强项。

状态门禁修正：

- 导出现在同时要求 `ReviewResult.passed=true`、无 `blockingIssues` 且章节处于 `DRAFT_READY/WAITING_APPROVAL`。
- 人工发布现在严格要求先有 `EXPORTED` 状态，并在确认事务中推进 `current_chapter`。
- 重复导出返回既有文件和 `idempotent=true`；无效状态不会覆盖已有稿件。
- 暂停小说拒绝新生成任务；`continue` 只创建当前下一章，连续 N 章也必须逐章人工确认后继续。
- 修改草稿会清空旧审查结果并回到 `REVIEWING`，必须重新审查后才能导出。

追加验证：

- `make lint && make typecheck && make test && make build`：13/13 测试通过。
- HTTP 冒烟：`GET /` 与 `POST /api/novels` 通过；过程创建的临时 SQLite 已移出仓库。
