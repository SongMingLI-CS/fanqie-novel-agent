# PROJECT AUDIT — fanqie-novel-agent (Novel Agent)

审计日期：2026-09-10 · 分支：`codex/novel-agent` · 版本：`0.3.0`

本文件是一次「从『看起来能运行』升级为真实数据驱动、真实后端支撑、真实持久化」审计的结果快照。
**文档不能代替代码**：本文件列出的每个修复都已在仓库中实现，并有测试与真实进程验证。

---

## 1. Architecture

### 1.1 技术栈（实测，非猜测）

| 维度 | 事实 |
| --- | --- |
| 语言 | Python ≥3.11（本机 3.13.2） |
| 运行时依赖 | 仅 `httpx>=0.27`、`tenacity>=8.2`（`pyproject.toml`） |
| 前端 | 无框架：`static/index.html` + `static/app.css` + `static/app.js`（原生 JS/DOM） |
| HTTP 服务 | 标准库 `http.server.ThreadingHTTPServer` + `BaseHTTPRequestHandler` |
| 数据库 | SQLite（WAL、`BEGIN IMMEDIATE`、每线程连接、`busy_timeout=5000`） |
| 迁移 | 无迁移框架：`CREATE TABLE IF NOT EXISTS` + `_ensure_column` 追加式演进 |
| 模型供应商 | DeepSeek（OpenAI 兼容，仅 HTTPS，key 只读进程环境变量） |
| 实时推送 | SQLite `events` 表作为跨进程事件总线 + SSE（`text/event-stream`） |
| 鉴权 | 可选 Bearer Token（`NOVEL_AUTH_TOKEN`，SHA-256 + `hmac.compare_digest`，失败节流） |
| 测试 | `unittest`，181 例，CI：ubuntu/windows × Python 3.12/3.13 |
| 部署 | 两个进程（server / worker）+ 运维 CLI（`novel-agent-ops`） |

### 1.2 数据链路

```
Browser (static/app.js)
  -> fetch /api/*            (Bearer token when NOVEL_AUTH_TOKEN set)
  -> ThreadingHTTPServer Handler.do_GET/do_POST/do_PATCH   [novel_agent/server.py]
  -> Store (SQLite, per-thread conn, BEGIN IMMEDIATE)      [novel_agent/store.py]
  -> chapters / jobs / usage / story_bibles / events / audit_log ...
Worker process [novel_agent/worker.py]
  -> Store.claim_job -> NovelService.process_stream -> AsyncLLMClient (httpx, SSE)
  -> AgentPipeline (outline -> chapter -> polish) + CheckpointRepository
  -> ReviewResult -> chapters/chapter_drafts/review_results/usage
  -> EventRepository.publish -> events table -> SSE -> Browser
```

外部依赖只有 **DeepSeek**（`DEEPSEEK_BASE_URL` + `DEEPSEEK_API_KEY`，必须由部署环境注入）。
不存在对象存储、支付、邮件、OAuth、向量库集成（也未被声称存在）。

---

## 2. Feature Matrix

等级：0=纯 UI，1=UI+mock，2=有 API 但数据假，3=真实 DB，4=持久化+错误处理，5=生产可用。

| Feature | Frontend | API | DB | 真实调用 | 持久化 | 错误处理 | 测试 | 等级 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 新建/列出/切换小说 | 有 | 有 | 有 | 有 | 有 | 有 | 有 | 5 |
| StoryBible 编辑（JSON + 逐字段表单） | 有 | 有 | 有（版本递增） | 有 | 有 | 有 | 有 | 5 |
| 章节生成（单次合成 / 多智能体） | 有 | 有 | 有 | 有（DeepSeek） | 有 | 有 | 有 | 4（需真实 Key） |
| 断点恢复 / 检查点 | — | 有 | 有 | 有 | 有 | 有 | 有 | 5 |
| SSE 实时流 + 逐字揭示 | 有 | 有 | 有 | 有 | 有 | 有（回退轮询） | 有 | 5 |
| 重新审查 / 批准 | 有 | 有 | 有 | 有（本地审查器） | 有 | 有 | 有 | 5 |
| 导出 TXT/MD/JSON/DOCX | 有 | 有 | 有（export_jobs） | 有 | 有 | 有（失败落 FAILED） | 有 | 5 |
| 整本导出 | 有 | 有 | 有 | 有 | 有 | 有 | 有 | 5 |
| 人工发布确认 | 有 | 有 | 有（publish_records/publish_jobs） | 有 | 有 | 有 | 有 | 5 |
| 草稿历史 / 差异 / 回滚 | 有 | 有 | 有（chapter_drafts） | — | 有 | 有 | 有 | 5 |
| 生成过程回放 | 有 | 有 | 有（events） | — | 有 | 有 | 有 | 5 |
| 任务取消 / 重试 / 暂停·继续 | 有 | 有 | 有 | 有 | 有 | 有 | 有 | 5 |
| 章节搜索 | 有 | 有（服务端 `?q=`） | 有 | — | — | 有 | 有 | 5 |
| 用量 / 指标 / 审计 | 有 | 有 | 有（usage/audit_log） | 有 | 有 | 有 | 有 | 5 |
| 无 Key 演示回放 | — | — | 有 | 真实流水线 + 确定性回放器 | 有 | 有 | 有 | 5（演示通道） |
| 番茄平台自动发布 | 无 | 无 | 无 | 无 | — | — | — | 未实现（外部阻塞，见 §7） |


---

## 3. Mock Audit

审计方法：全仓库检索 `mock|fake|faker|dummy|sample|placeholder|fixture|stub|demo|hardcoded|TODO|FIXME|HACK|not implemented|Math.random|setTimeout`，
并逐个回溯引用链（含 `server`/`lib`/`utils`/`hooks`/`stores`/`contexts`）。

**结论：生产路径中不存在 mock / fake / 占位数据。**

- `Math.random`、用 `Date.now` 造假数据、把 `localStorage` 当业务存储：生产代码 **0 处**。
- 所有统计（`/api/ops/metrics`、`/api/ops/usage`）均为真实 SQL 聚合（`COUNT`/`SUM`/`GROUP BY`），
  无随机数、无估算值。
- 时间戳一律来自数据库 `created_at`/`updated_at`，无 `new Date()` 冒充创建时间。
- 前端 `localStorage` 仅用于：访问凭证、深色主题、上次阅读位置 —— 属允许的「用户偏好」。

需要保留的「模拟」及其隔离边界：

| 位置 | 性质 | 是否在生产路径 | 处理 |
| --- | --- | --- | --- |
| `novel_agent/replay.py` | 确定性模型回放器（录制/回放） | 否，仅 `demo.py` CLI 使用 | 保留；走真实流水线，只替换模型 |
| `novel_agent/demo.py` | 无 Key 演示入口（独立子命令） | 否，需显式 `python -m novel_agent.demo` | 保留 |
| `tests/**` 的 `FakeClient` 等 | 测试夹具 | 否 | 保留（测试专用） |
| `publishers.py` 的 `DryRunPublisher`/`LocalFilePublisher`/`ExternalPublisher` | 未来平台适配器接缝 | **否（当前无生产调用方）** | 保留为契约，并已在文档如实标注「运行时导出走 `exporters.py`」 |

---

## 4. API Audit

全部路由在 `novel_agent/server.py` 中显式实现；不存在「返回硬编码对象却不做业务」的假端点。
前端调用的每个端点都追溯到真实 service/store 调用。

| Endpoint | 方法 | 校验 | 鉴权 | Store/Service | DB | 真实 |

---

## 5. Database Audit

表：`novels`、`story_bibles`、`chapters`、`characters`、`world_rules`、`timeline_events`、
`foreshadowing`、`jobs`、`usage`、`chapter_drafts`、`review_results`、`publish_records`、
`export_jobs`、`publish_jobs`、`audit_log`、`agent_runs`、`agent_stages`、`checkpoints`、`events`。

CRUD 真实性：Create/Read/Update/Delete 均有真实 SQL（见 §4）。`characters`/`world_rules`/
`timeline_events`/`foreshadowing` 由 `_sync_bible` 从 StoryBible 派生写入，属有意的去规范化镜像；
审查器读取 `story_bibles` 中的权威内容，不会误判已登记的本地角色。

约束与索引：`UNIQUE(novel_id,number)`、`UNIQUE(chapter_id,version)`、`UNIQUE(novel_id,key)`、
`UNIQUE(idempotency_key)`；`PRAGMA foreign_keys=ON`；索引 `idx_audit_created`、`idx_usage_created`、
`idx_events_novel`、`idx_events_chapter`、`idx_events_created`（本次新增）。

本次修复：

- `store.recent` 由「读全表再切片」改为 `ORDER BY number DESC LIMIT ?`（可选 `before` 参数）。
- `store.chapters` / `published_chapters` / `novels` 由 N+1 改为单条 SQL；抽出 `_hydrate_chapter` 统一解码。
- `usage_series` 改为可走索引的 `created_at >= ?`。
- 新增 `events(created_at)` 索引：保留期删除不再全表扫描。
- `export_jobs` 失败不再滞留 `PENDING`（新增 `fail_export_job`）。
- 终态章节冻结（`update_draft` 拒绝 `PUBLISHED_MANUALLY` / `CANCELLED`）。

未做（有意）：未 reset 任何生产库；全部演进均为追加式（`IF NOT EXISTS` / `ADD COLUMN` / 新索引）。

---

## 6. Security Audit

| 项 | 结论 |
| --- | --- |
| API Key 泄漏 | `DEEPSEEK_API_KEY` 仅服务端 `os.environ` 读取；前端 bundle 不含任何服务端密钥 |
| Secret 入库 | 否（`usage`/`audit_log` 不含密钥；日志只记录 category/status/耗时） |
| 鉴权 | 可选 Bearer，常数时间比较 + 失败指数退避；`/api/*` 全覆盖（含 `/api/ops/*`） |
| 权限（IDOR） | 单用户部署模型：无 per-user 资源，`userId` 从不来自请求体 |
| 状态门禁 | 批准/导出/发布/回滚**全部服务端校验**（本次补上 approve 的 `review.passed` 与终态冻结） |
| 路径穿越 | 静态文件 `resolve()` + 前缀校验 + `\x00` 拒绝 |
| 请求体 | 上限 1MB（413），负长度拒绝，非 JSON/非对象拒绝 |
| 响应头 | `Cache-Control: no-store`、`X-Content-Type-Options: nosniff`、`X-Frame-Options: DENY`、`Referrer-Policy: no-referrer` |
| SQL 注入 | 全部参数化查询；表名/列名来自代码常量白名单 |
| SSRF | 出站仅 `DEEPSEEK_BASE_URL` 且强制 HTTPS |
| 传输安全 | `ssl.create_default_context` + 可选 CA bundle；不关闭证书校验 |

---

## 7. External Blockers

| 阻塞项 | 影响 | 当前状态 |
| --- | --- | --- |
| `DEEPSEEK_API_KEY`（付费/账号） | 无法真实生成章节 | 代码已推进到「只差 Key」：缺配置时明确失败（绝不伪造结果），错误码与前端修复指引齐备 |
| 番茄小说等平台正式发布 API/授权 | 无法自动发布 | 未实现，也不做浏览器点击/登录/验证码绕过；仅有 `ExternalPublisher` 契约 + 人工发布确认流程 |
| 生产 CA bundle（受限环境） | TLS 失败 | 通过 `DEEPSEEK_CA_BUNDLE` 注入，未关闭校验 |
| 多实例部署的数据库/队列 | SQLite 适合单机/低并发 | 已在部署文档声明边界，repository 边界保留 |

---

## 8. Completed Work

- `novel_agent/reviewer.py`：新增 `chapter_as_output()`（DB 行 → 模型输出契约投影）。
- `novel_agent/server.py`：重新审查走投影 + `before` 窗口 + 目标字数；`approve` 要求 `review.passed`；
  `config` 先校验后建任务；导出失败落 `FAILED` 并返回稳定错误码；章节列表支持 `light`/`q`/`limit`。
- `novel_agent/store.py`：`_hydrate_chapter` 统一解码；`recent(before=)`、`chapters`、`published_chapters`、
  `novels`、`chapter_summaries(query,limit,offset)` 全部改为单条有界 SQL；`usage_series` 走索引；
  新增 `events(created_at)` 索引与 `fail_export_job`；`record_review` 不降级已导出/终态；
  `update_draft` 终态冻结；`cancel_job`/`rewrite_chapter` 改用 `models.ACTIVE`。
- `novel_agent/service.py`：`context()` 使用 `recent(before=number)`。
- `novel_agent/stages.py`：复用 `AgentContext.load_memory`（去掉两处重复 JSON 解析）。
- `novel_agent/deepseek.py` / `llm.py`：`prompt_version` 统一取自 `prompts.VERSION`。
- `novel_agent/demo.py` / `events.py` / `worker.py`：删除无引用符号。
- `static/app.js`：章节列表 `?light=1&limit=500`、正文按需取回并按 `updated_at` 失效缓存；
  超窗口时走服务端 `?q=` 搜索；`hasBody`/`contentLen` 语义统一；梗概兼容 `goal`/`hook`；新增错误码文案。
- 文档：`README.md`、`docs/novel-agent-progress.md`、`docs/novel-agent-data-model.md`、
  `docs/novel-agent-publishing.md` 同步更新；新增本文件。

---

## 9. Remaining Issues

P0：无。
P1：无。

P2（已知、可接受、非阻塞）：

- `NOVEL_AUTH_TOKEN` 为单共享密钥并存于浏览器 `localStorage`；未做多用户/OAuth（与单机部署模型一致）。
- 单次模型调用内部不做 token 级续接（生成式模型无法安全续写），恢复粒度是一个已完成并落检查点的节点。
- SQLite 适合单机/低并发；多实例需换数据库/队列。
- 无 CSP（前端使用内联 `onclick`，加 CSP 需 `unsafe-inline`，收益有限）。

P3：

- `audit_log` 仅支持 `limit`，无游标分页；数据量极大时可加 `rowid` 游标。
- `publishers.py` 三个类当前无生产调用方（作为平台接入接缝保留，已在文档如实标注）。
- 章节正文同时存在于 `chapters`（当前投影）与 `chapter_drafts`（历史版本），属有意的追加式设计。

---

## 10. Verification Evidence

| 项目 | 命令 | 结果 |
| --- | --- | --- |
| Lint / typecheck | `python -m compileall -q novel_agent tests static`、`git diff --check` | 通过 |
| 前端语法 | `node --check static/app.js` | 通过 |
| 单元 + 集成 | `python -m unittest discover -s tests -v` | **181 tests, OK**（退出码 0） |
| 新回归测试 | `python -m unittest tests.test_review_flow -v` | 22 tests, OK |
| 端到端持久化 | `python -m unittest tests.test_e2e_persistence -v` | 1 test, OK（真实子进程 + 重启） |
| 无 Key 演示（真实流水线） | `python -m novel_agent.demo replay --quiet` | `ok=true, WAITING_APPROVAL, 34 events` |
| 环境 | Python 3.13.2 / Node 24.11.1 / Windows | — |

---

## 11. Production Readiness

| 维度 | 评分 | 说明 |
| --- | --- | --- |
| Frontend | 88 | 原生 JS 无构建；加载/空/错误态完整；流式、回放、差异、表单齐备；缺 CSP 与浏览器自动化测试 |
| Backend | 90 | 显式路由 + 服务端门禁 + 幂等 + 租约/重试/超时 + 统一错误码；单进程 HTTP 服务器（非 ASGI） |
| Database | 90 | SQLite/WAL/显式事务/唯一约束/索引齐备；无迁移版本表（追加式演进）；单机定位 |
| Auth | 78 | 可选 Bearer + 常数时间比较 + 节流；单共享密钥，无多用户/RBAC |
| Storage | 85 | 章节/草稿/导出/发布/审计全持久化；导出文件为本地磁盘（无对象存储，也未声称） |
| AI | 88 | 真实 DeepSeek 调用（流式、thinking 开关、超时/重试/错误分类）+ 多智能体 + 检查点恢复；需用户 Key |
| Testing | 90 | 181 例含真实 HTTP 与真实子进程重启持久化；无浏览器 E2E |
| Security | 86 | 无密钥泄漏、参数化 SQL、路径穿越防护、安全响应头；无 CSP/CSRF token（无 Cookie 会话，风险低） |

**Overall Production Readiness：87 / 100**（单机/内网部署可生产使用；横向扩展与平台自动发布需外部条件）

| --- | --- | --- | --- | --- | --- | --- |
| `/healthz`, `/readyz` | GET | — | 公开 | `SELECT 1`（readyz） | 有 | 有 |
| `/api/novels` | GET/POST | title 必填、bible 必须对象 | 有 | `novels`/`create_novel` | 有 | 有 |
| `/api/novels/<id>` | GET | 404 | 有 | `get_novel` | 有 | 有 |
| `/api/novels/<id>/chapters` | GET | `light`/`q`/`limit` 校验 | 有 | `chapters`/`chapter_summaries` | 有 | 有 |
| `/api/novels/<id>/chapters/generate` | POST | chapterNumber、config **先校验** | 有 | `create_job`（幂等） | 有 | 有 |
| `/api/novels/<id>/continue` | POST | 同上 + 串行确认门禁 | 有 | `set_paused`+`create_job` | 有 | 有 |
| `/api/novels/<id>/pause` | POST | 404 | 有 | `set_paused` | 有 | 有 |
| `/api/novels/<id>/story-bible` | PATCH | 必须对象 | 有 | `update_bible`（版本递增） | 有 | 有 |
| `/api/novels/<id>/export-book` | POST | format ∈ {txt,md} | 有 | `published_chapters`+`export_book` | 有 | 有 |
| `/api/novels/<id>/jobs` | GET | status/limit | 有 | `jobs` | 有 | 有 |
| `/api/novels/<id>/usage` | GET | 404 | 有 | `usage` | 有 | 有 |
| `/api/novels/<id>/runs/latest` | GET | 404 | 有 | `CheckpointRepository` | 有 | 有 |
| `/api/novels/<id>/chapters/<n>/timeline` | GET | limit≤10000 | 有 | `chapter_events` | 有 | 有 |
| `/api/chapters/<id>` | GET/PATCH | 字段白名单 + **终态冻结** | 有 | `chapter_by_id`/`update_draft` | 有 | 有 |
| `/api/chapters/<id>/history` | GET | 404 | 有 | `draft_history` | 有 | 有 |
| `/api/chapters/<id>/rollback` | POST | version、非终态 | 有 | `update_draft` | 有 | 有 |
| `/api/chapters/<id>/review` | POST | 404 | 有 | `chapter_as_output`+`review`+`record_review` | 有 | 有 |
| `/api/chapters/<id>/approve` | POST | **要求 review.passed** | 有 | `set_status` | 有 | 有 |
| `/api/chapters/<id>/export` | POST | format、ready 门禁 | 有 | `create_export_job`+`export_chapter` | 有 | 有 |
| `/api/chapters/<id>/publish` | POST | platform/operator 必填、须 EXPORTED | 有 | `manual_publish`（事务） | 有 | 有 |
| `/api/chapters/<id>/rewrite` | POST | 最新未发布章 | 有 | `rewrite_chapter`（事务） | 有 | 有 |
| `/api/jobs/<id>`, `/api/jobs/<id>/cancel` | GET/POST | 404 | 有 | `get_job`/`cancel_job` | 有 | 有 |
| `/api/events` | GET | novel_id 必填 | 有 | SSE + `read_since` 游标 | 有 | 有 |
| `/api/ops/metrics` `/usage` `/audit` | GET | days≤90, limit≤1000 | 有 | 真实聚合 | 有 | 有 |
