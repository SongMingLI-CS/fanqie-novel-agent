# 小说自动创作智能体进度

## 当前状态

- 分支：`codex/novel-agent`
- 开始日期：2026-09-02
- 当前阶段：多智能体流式写作 v0.3.0 + 无 Key 演示回放/Windows 优雅停止/DOCX + 草稿历史与生成回放 UI + CI
- 最近提交：8a9e416

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

## 2026-09-04：前端全面重做（错误清晰化 + 界面美化）

状态：完成。后端 API 与状态机保持不变，仅重写 `static/index.html`（单文件，约 176 → 1032 行）。

- 诊断根因：当时 `.env` 中 `DEEPSEEK_API_KEY` 为空，生成任务连续失败 6 次、错误为 `DEEPSEEK_API_KEY is not configured`；旧页面既无轮询也不显示 Job 错误，用户在浏览器上完全看不到原因，因此误以为系统不可用。
- 新增「问题横幅」：由失败的 Job/Usage 自动推导展示位（缺 API Key → 醒目配置类横幅，给出补齐 `.env`、重启 server/worker 的分步修复指引）；连接状态、鉴权（Bearer 令牌）、深浅色主题均可在页头切换并记忆。
- 状态徽章：章节与任务按状态着色（`PLANNING/GENERATING/DRAFT_READY/WAITING_APPROVAL/FAILED…`），章节卡片内直接展示审查结果（得分/issue/blocking）、字数、时间；错误框内联在对应卡片上，并提供「重试生成」。
- 任务看板：展示 Job 尝试次数、错误与队列时间；用量面板展示每次调用模型/Token/耗时/状态。
- 自动轮询刷新：有活跃 Job 时 1500ms、空闲 4000ms，`document.hidden` 时暂停；带变更检测避免无效重渲染。
- 错误语义：`explainError()` 把后端机器错误串映射为中文分类（配置/http 401/429/dns/tls/连接超时/暂停等）与修复建议；审查阻断码也有对应中文提示。
- 验证：`node --check` 通过（33KB）；无头 DOM 桩连真实 API 冒烟通过——`boot-ok`、横幅命中「缺少 DeepSeek API Key」、章节/Job/用量面板均渲染出失败章节与配置错误、连接状态「服务已连接」。

## 2026-09-04：真实调用排障闭环（API Key → TLS → Reviewer 崩溃 → 横幅误报）

状态：完成。真实小说 `7bffa7be-3f2a-4bc2-b7ea-b9169ea99f6d`（穿越异世界锻炼成为最强的我…）第 1 章已端到端真实生成成功；前端横幅对「已修复的历史失败」不再误报。

- **根因一（API Key“读不到”）**：`.env` 第 1 行 `DEEPSEEK_API_KEY` 磁盘上确实为空（编辑缓冲未保存）。用户按 `Cmd+S` 保存后验证长度为 35；环境变量只在进程启动时读取，因此保存后必须重启 server 与 worker 才生效。
- **额外配置**：`.env` 中意外出现非空 `NOVEL_AUTH_TOKEN` 导致全部 API 返回 401；按用户说明清空并重启，恢复免鉴权本机访问。
- **根因二（TLS 证书失败 `tls_error`）**：Homebrew Python 缺少 CA bundle（`/opt/homebrew/etc/openssl@3` 为空、未装 certifi），`SSLCertVerificationError: unable to get local issuer certificate`；`/etc/ssl/cert.pem` 验证 DeepSeek（TLS 1.3）通过。客户端本就支持 `DEEPSEEK_CA_BUNDLE`（`deepseek.py` 的 `ssl.create_default_context(cafile=...)`），故只在 `.env` 写入 `DEEPSEEK_CA_BUNDLE=/etc/ssl/cert.pem` 后重启即修复，无代码改动。
- **根因三（Reviewer 崩溃，内容丢失）**：该小说 StoryBible 使用 `protagonist/mainCharacters/storyArcs` 字段集，而 `service.compact_bible()` 固定输出另一套键并把缺失字段填 `None`；`reviewer.review()` 迭代 `bible.get('characters',[])` 等时对 `None` 抛 `TypeError`，导致一次已成功付费调用的产物在 `save_generation` 前丢失，Job 卡在 `RUNNING`、章节卡在 `REVIEWING`。修复：在 `reviewer.review()` 顶部把任意非 list 的 bible 区块防御性置空列表；该真实 Job 已通过 `POST /api/jobs/<id>/cancel` 恢复后重新排队。
- **前端横幅误报修复**：用量日志是追加式的，历史失败会一直保留；旧 `updateBanner()` 会把这些历史失败当作“当前问题”持续弹「缺少 DeepSeek API Key——生成功能当前不可用」。新增按 Job 判定的“已解决”规则：同一 Job 若存在更新的 `succeeded` 用量记录，则该 Job 更早的失败不再作为横幅来源；真正仍未解决的失败（如从未成功、或成功之后再次失败）仍会照常展示。为匹配该语义，把原 `test_api_key_not_in_frontend`（断言 HTML 不含字面 `DEEPSEEK_API_KEY`）改为 `test_frontend_exposes_no_api_secret`（页面允许出现环境变量名用于修复指引，但绝不允许真实/形似 `sk-…` 的密钥）。
- 验证：
  - 真实第 1 章《穿越之始》已保存为 `WAITING_APPROVAL`，正文 1,569 字符；自动审查通过、得分 100、无 blocking；Job `16863d63…` 状态 `SUCCEEDED`（attempts=10）；用量成功记录 `deepseek-v4-flash`（in 1,320 / out 1,612 / 22,056ms）。
  - `python3 -m unittest discover -s tests`：41/41 通过；`python3 -m compileall -q novel_agent tests` 通过。
  - 无头 DOM 桩强制选中该真实小说后：`banner-len=0`、无「缺少 DeepSeek API Key」；章节卡片渲染《穿越之始 / 待人工批准 / 1,569 字 / deepseek-v4-flash》。
  - 负向控制：从未成功的小说 `7e90571e-…`（历史 `DEEPSEEK_API_KEY is not configured`）仍正确弹出配置横幅，证明修复不会掩盖仍存在的真实问题。
- 备注（未处理的设计漂移）：`compact_bible()` 用固定键集压缩 StoryBible，会丢弃该小说真实的 `mainCharacters/protagonist` 等字段并注入 null，使模型缺少角色上下文；reviewer 防御补丁只保证不崩溃，未登记角色仍只产生 warning。后续建议让压缩与审查直接消费小说原生 bible 键，而非固定模板键。

## 2026-09-04：正确性 → 可观测性 → 安全三波收尾（工业级化）

状态：完成。按用户「以上全部，按正确性→可观测性→安全的顺序逐个推进」推进，验证后共跑通模块测试与进程级 HTTP 测试。

### ① 正确性：消除 compact_bible / reviewer 的模板键设计漂移

针对上面「未处理的设计漂移」备注做根治，而非再加防御补丁：

- `service.compact_bible()` 重写：不再用固定模板白名单，改为**保留 StoryBible 全部原生顶层键**的有界递归截断（list 取前 20、str 取前 2000、dict 递归所有键）；仅在整体超过预算（默认 14000 字符）时降级为 `{'contextTruncated': True, 'facts': 截断串}`，不再注入任何 `None` 模板键。
- `service.process()` 审查阶段改为对**完整权威原生 bible**（`novel['story_bible']`，不截断）做一致性校验，而不是压缩摘要；提示词仍用有界压缩版。
- `reviewer.review()` 重写角色/规则检查：新增 `_walk()` 与 `_registered_names()`，用 set 成员判断识别原生 schema 的角色（`protagonist` dict、`mainCharacters`、`supportingCast` 等任意含 `name` 的节点），消除对原生角色 `unregistered_character` 的误报；`unauthorized_world_rule` / `timeline_event_redefinition` / `foreshadowing_not_open` 等规范冲突仍按 key 阻断。
- 回归测试 6 条（原生键保留/不注入 null/超限降级/原生角色被识别/规范冲突仍阻断/端到端 process 用原生 bible 审查）全部通过。

### ② 可观测性：审计轨迹 + 运维指标 + DB 维护/备份 CLI

- `store.py` 新增只读 `audit_log` 追加表与 `record_audit()`（尽力而为，审计写失败绝不影响主操作），并在 create_novel / update_bible / cancel_job / rewrite_chapter / record_export / manual_publish 记录 `novel_created / bible_updated / job_cancelled / chapter_rewritten / chapter_exported / chapter_published`。
- 新增只读聚合 `Store.metrics()`（novels/chapters/jobs/usage/byModel/exports/publishes/storyBibleVersions/auditEvents）与 `Store.usage_series(days)`（按日、零填充、窗口 1–90 天）与 `Store.audit_trail(limit)`。
- 新增鉴权保护（`/api/*` 之下）的 `GET /api/ops/metrics`、`/api/ops/usage?days=`、`/api/ops/audit?limit=`。
- 新增零依赖运维 CLI `novel_agent/ops.py`（注册 `novel-agent-ops` console script）：`stats`（打印指标+日趋势+磁盘占用）、`backup`（SQLite 在线备份 API，可边跑边备份，`--force` 覆盖）、`vacuum`（WAL checkpoint + VACUUM）。
- 测试：Store 层 10 条（审计顺序/限长/指标/发布与导出/日趋势）+ HTTP `/api/ops/*` 端点测试全部通过。

### ③ 安全：鉴权加固、响应头、请求体上限、路径穿越复核、密钥脱敏复核

- 请求体上限复核：`_read_body` 既有 1MB/413 `payload_too_large` 上限；新增**负 Content-Length 拒绝**（400），防止 `read(负数)` 读到 EOF 造成挂起。
- 鉴权加固（`auth.py` 重写）：Bearer 比较改为**对两侧 SHA-256 摘要做 `hmac.compare_digest`**，不泄露令牌长度；前缀必须严格 `Bearer `；新增**按客户端进程内失败节流**（60 秒窗口内第 5 次起指数退避 sleep，封顶 2 秒，成功后清空该客户端计数），`reset_throttle()` 供测试与轮换令牌后调用。
- 安全响应头：所有 JSON 与静态响应加 `X-Content-Type-Options: nosniff`、`X-Frame-Options: DENY`、`Referrer-Policy: no-referrer`（`Cache-Control: no-store` 原本已有）。
- 路径穿越复核：`_serve_static` 的 `resolve()` 包含性检查正确；补 `\x00` 空字节守卫（404），并加编码穿越回归测试（`%2e%2e`/`..%2f` 均 404，绝不泄漏 static 之外文件）。
- 密钥脱敏复核：`Config.__repr__` 只显示 `auth=on/off`；请求日志仅 method/path/status/duration，不落鉴权头/请求体；DeepSeek 日志不记录 API key（既有测试 `test_failure_logs_never_include_api_key` 与 `test_repr_hides_no_auth_state` 继续通过）。
- 测试：`tests/test_auth.py` 6 条单测 + HTTP 层（配置令牌后 API 401/200、健康与静态保持开放、413 上限、安全头、路径穿越）全部通过。

## 2026-09-06：多智能体流水线、断点恢复与异步流式交付（v0.3.0，提交 589f4de）

状态：完成。自 61fff49（控制台加固）之后的整段未提交增量一次落盘：多智能体
流水线与异步 DeepSeek/SSE 流式在 `service.py`/`server.py`/`worker.py`/`static/*`
等共享文件中深度交织，无法在不破坏中间状态的前提下拆成两个可独立运行的提交，
因此作为一个整体提交（32 文件，+3369/−1095），冒烟与测试均针对该最终态。

### ① 多智能体责任链 + 断点恢复（含提示词治理）

- `prompts/`：全部 system/user/repair 提示词外置为 Markdown（`outline/chapter/
  polish/repair/system/request`），`prompts.py` 的 `PromptManager` 负责惰性加载、
  `<<var>>` 占位符严格渲染校验，并落 `usage.prompt_version = novel-writer@2`。
- `stages.py`/`checkpoints.py`/`context.py`：`outline → chapter → polish`
  责任链，`AgentContext` 记忆逐节点传递；每个节点完成后把累计上下文写入新增的
  `agent_runs`/`agent_stages`/`checkpoints` 三表（每线程连接 + `BEGIN IMMEDIATE`）。
  worker/服务重启后按 `NOVEL_AUTO_RESUME`（默认开）从未完成节点续跑，已完成节点
  不重复调用模型。`models.py` 增 `RunStatus`/`StageState`；`deepseek.py` 增
  `validate_outline_output`；`GET /api/novels/<id>/runs/latest` 暴露最近一次 run 与
  各阶段状态；`generate`/`continue` 接受 `config.{stages,targetWords,autoExportTxt}`。

### ② 异步模型调用：httpx + tenacity

- `llm.py` 新增 `AsyncLLMClient`：httpx 全异步流式（`stream + include_usage`），
  错误分类与同步 `deepseek.py` 一致；tenacity 指数退避重试，429 优先尊重
  `Retry-After`、退避封顶 30s、`NOVEL_MAX_RETRIES` 控预算；配置类/400/401/404/
  中途断流与截断**不重试**（避免重复计费）。`stream()` 逐块产出 `delta` → `usage`。

### ③ 跨进程事件总线 + SSE 实时流 + 逐字打印

- `events.py` 新增 `EventRepository`：`events` 自增游标表，`publish/read_since/
  latest_id/prune`，跨 server/worker 进程；`service.process_stream`（async）按
  ~100 字/300ms 节流发布 `llm.delta`，完整校验后发布 `llm.text`（已验证正文），
  并发布 `agent.stage/run`、`checkpoint.saved`、`chapter.ready`、`job.status`；
  同步旧路径零改动保留。
- worker 主循环切到 `run_once_stream`（线程内 `asyncio.run`），启动时清理过期事件；
  server 增 `GET /api/events?novel_id&since` SSE 长连接（0.4s 轮询 + 15s 心跳注释、
  断线按 since 游标续读）；`NOVEL_EVENT_TTL_DAYS`（默认 7）控保留天数。
- 前端：`static/` 重构为 index.html + app.css + app.js；SSE 客户端用 fetch +
  ReadableStream 解析帧并携带 `Authorization`，切换小说/改凭据自动重连，断线由轮询
  兜底；`llm.delta` 实时驱动 token 计数与阶段时间线，`llm.text` 触发打字机逐字揭示
  （闪烁光标），阶段事件即时刷新左侧 Agent 时间线。

### ④ 验证

- 定向复跑全绿：`test_events` 4、`test_llm_async` 7、`test_streaming` 4、
  `test_pipeline` 4、`test_prompts` 6、`test_repo_and_config` 5、`test_config` 12；
  `compileall` 与 `git diff --check` 通过。
- 冒烟：服务端正常启动，`/api/events` 缺 `novel_id` 返回 400，前端静态资源 200；
  SSE 端点真连接实测逐帧与游标语义通过（`test_streaming`）。

### Windows 本机测试修正（提交 f32a54b / 7110975）

2026-09-06 深夜把 Windows 下剩余的 4 个环境性失败全部处理为可运行/如实标注：

- **3 个 GBK 失败**：`test_novel_agent.py` 中对 UTF-8 导出/静态文件的读取显式加
  `encoding="utf-8"`（导出 TXT 2 处、`static/index.html` 1 处），Windows 默认 GBK
  不再误读。
- **`test_deepseek_timeout_retries`**：根因是 `Config(...)` 硬编码 Linux 路径
  `ca_bundle='/etc/ssl/cert.pem'`，Windows 上 `ssl.create_default_context` 抛
  `FileNotFoundError`（`OSError` 子类）被误分类为 `tcp_connection_error` 且 3 次
  重试全败；去掉该 Linux 专属参数后测试在注入 opener 上跨平台通过（非网络依赖）。
- **`test_http_ops` 12 个进程测试**：`python3` 命令改为 `sys.executable`
  （Linux 下同值，纯跨平台改进），子进程在本机即可拉起；随后 server/worker 注册
  `SIGBREAK`，Windows 用例改用 `CREATE_NEW_PROCESS_GROUP` + `CTRL_BREAK_EVENT`
  真实验证优雅退出（rc=0），两个用例不再跳过。

验证：Windows 本机全量 `python -m unittest discover -s tests` = **Ran 145 tests,
OK（无跳过）**；`compileall` 与 `git diff --check` 通过。另注：运行 v0.3.0 前需
`python -m pip install -e .`（自动带 `httpx`/`tenacity`）。



## 2026-09-06（深夜）：三项功能补强（无 Key 演示回放 / Windows 优雅停止 / DOCX 导出）

### ① 无 Key 端到端演示：录制 + 回放

- `novel_agent/replay.py`（新增）：`Recording`（v1 JSON：小说快照 + 有序模型调用
  `system/user/text/usage`）、`CapturingAsyncClient`（包一层真实异步客户端录制
  每次调用）、`ReplayClient`（既当同步又当异步客户端按序回放；record 文件默认严格
  校验提示词一致，`loose` 可仅按顺序）。
- `novel_agent/demo.py`（新增）：`python -m novel_agent.demo replay` 用内置演示
  录制跑通真实 `service.process_stream` 流水线（outline→chapter→polish、检查点、
  `llm.delta/llm.text`、审查门禁），headless 打印事件时间线并在终端逐字揭示正文；
  `--port` 时复用 `server` 模块全局注入起真实 HTTP/SSE，浏览器可直接看打字机；
  `record` 子命令带真实 Key 生成 replay 文件。
- 不插桩生产代码：worker/server/真实客户端零改动；测试 `tests/test_demo.py` 8 例
  （录制结构、严格/宽松回放、耗竭报错、headless 端到端、HTTP 模式 URL）。

### ② Windows 优雅停止（SIGBREAK）

- `server.py`/`worker.py` 的 main 在 `SIGTERM`/`SIGINT` 之外同时注册 `SIGBREAK`
  （仅 Windows 存在）；`tests/test_http_ops.py` 用 `CREATE_NEW_PROCESS_GROUP`
  拉起子进程，Windows 侧发 `CTRL_BREAK_EVENT`、POSIX 侧发 `SIGTERM` 统一验证
  rc=0。2 个原被跳过的优雅退出用例现跨平台真跑，**无 skip**。

### ③ DOCX 导出（零依赖 OOXML）

- `exporters.py`：`EXPORT_FORMATS` 增加 `docx`，`_docx_bytes()` 用标准库
  `zipfile` 生成最小合法 OOXML（`[Content_Types].xml` / `_rels/.rels` /
  `word/document.xml`；标题与卷章行加粗、正文按空行分段、XML 转义），原子写盘
  与既有导出一致；`server.py` 的 `EXPORT_FORMATS` 改为从 exporters 引用，API
  与导出按钮均支持 `format=docx`。
- 测试：`test_export_docx_is_valid_ooxml_with_escaped_text`（zip 结构、转义、
  无 tmp 残留）。

### 验证

Windows 全量 `python -m unittest discover -s tests` = **Ran 145 tests, OK**；
`compileall`、`git diff --check` 通过。文档同步：README（demo/优雅退出/DOCX）、
`docs/novel-agent-publishing.md`、`docs/novel-agent-deployment.md`。

## 2026-09-07：草稿历史 / 生成回放 UI + CI

### ① 草稿版本历史、差异对比与回滚

- `store.py`：`draft_history(cid)`（每版 payload + 关联 review_results，按版本号升序）、
  `draft_version(cid, version)`。
- API：`GET /api/chapters/<id>/history`、`POST /api/chapters/<id>/rollback`
  （回滚=取旧版字段经 `update_draft` 生成**新版本**并回到 `REVIEWING`，不清历史；
  已人工发布章节 409 拒绝并落 `chapter_rollback` 审计）。
- 前端阅读区「📑 历史」：版本列表（模型生成/手动编辑标识 + 审查徽标）、任意版本预览、
  与当前稿**逐行 LCS 差异**（红=仅当前，绿=仅所选版本）、一键回滚。

### ② 过往生成过程回放

- `events.py`：`chapter_events(novel_id, number)`（按 `json_extract(payload,'$.chapter')`
  过滤该章全部事件，SSE 原序）。
- API：`GET /api/novels/<id>/chapters/<n>/timeline`。
- 前端阅读区「🎞 回放」：弹层按事件原序重现 `agent.stage / checkpoint.saved /
  llm.delta / llm.text / chapter.ready`，日志滚动 + 正文打字机揭示，支持暂停/继续/关闭。

### ③ CI（`.github/workflows/tests.yml`）

- push / PR 触发；`ubuntu-latest` + `windows-latest` × Python 3.12/3.13 矩阵：
  `pip install -e .` → compileall → `unittest discover -s tests` → `git diff --check`。
- Windows 无真实控制台环境（如 CI runner）无法投递 `CTRL+BREAK` 时，两个优雅停止用例
  改为 `skipTest` 如实跳过（本地交互控制台仍实测 rc=0 通过）。

### 验证

Windows 全量 `python -m unittest discover -s tests` = **Ran 150 tests, OK**；
`test_http_ops` 12/12 OK、`test_history_api` 5/5 OK；`compileall`、
`git diff --check` 通过；`static/app.js` 通过 `node --check`。

