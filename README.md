# Novel Agent

一个由项目自身服务端、SQLite 数据库和可恢复 Worker 驱动的长篇小说创作智能体。浏览器只调用本地 API；DeepSeek key 只从服务端环境读取。

## 运行

server 与 worker 启动时会自动加载 `.env`（查找顺序：`NOVEL_ENV_FILE` → 当前目录 `.env` → 仓库 `.env`），无需手动 `source`；进程已有的环境变量始终优先于文件，文件不存在也不报错。配置解析失败会在启动时抛出 `ConfigError`，带病的配置不会被静默忽略。

方式 A：直接以模块运行（v0.3.0 起先安装两个运行时依赖 `httpx`、`tenacity`，或直接用方式 B 的可编辑安装自动带入）：

```bash
python3 -m novel_agent.server
# 另一个终端启动可恢复 Worker
python3 -m novel_agent.worker
```

方式 B：可编辑安装后使用 console script（`make install` = `python3 -m pip install -e .`）：

```bash
novel-agent-server
novel-agent-worker
novel-agent-ops       # stats / backup / vacuum
```

打开 <http://127.0.0.1:8787>（监听地址/端口由 `NOVEL_HOST`/`NOVEL_PORT` 控制；`NOVEL_PORT=0` 表示随机空闲端口，进程会在 stdout 打印 `NOVEL_AGENT_LISTENING host:port`）。首次创建小说时会读取 `.agents/skills/novel-writer/references/` 下的四份 Skill 参考文件并保存初始 StoryBible 版本。

### 运维接口与优雅退出

- 健康检查（无需鉴权）：`GET /healthz` 存活探测恒返回 200；`GET /readyz` 就绪探测会额外对 SQLite 执行 `SELECT 1`，数据库不可用时返回 503 `store_unavailable`。
- 优雅退出：server 与 worker 收到 `SIGTERM`/`SIGINT` 后停止接收新请求/任务，收尾后以退出码 0 结束，适合被 systemd/launchd/容器托管；Windows 下同样注册 `SIGBREAK`（控制台 `CTRL+BREAK`），可用 `CREATE_NEW_PROCESS_GROUP` + `CTRL_BREAK_EVENT` 实现跨平台优雅停止（回归测试已覆盖）。
- 结构化日志：`NOVEL_LOG_FORMAT=text|json`（JSON 每行一个对象并合并 job/chapter 等字段），`NOVEL_LOG_LEVEL` 控制级别；每条 HTTP 请求记录 method/path/status/duration_ms。
- 运维指标（与 `/api` 一样受鉴权保护）：`GET /api/ops/metrics` 聚合快照、`GET /api/ops/usage?days=N`（≤90）按日零填充趋势、`GET /api/ops/audit?limit=N`（≤1000）审计轨迹（新在前）；写操作自动落审计：建书/改 Bible/取消任务/重写章节/导出/人工发布。
- DB 维护 CLI：`python3 -m novel_agent.ops stats --db data/novel.sqlite3`（打印指标+日趋势+磁盘占用）、`backup --db … --out … [--force]`（SQLite 在线备份，可边运行边备份）、`vacuum --db …`（WAL checkpoint + VACUUM）。也可用 `novel-agent-ops` 等 console script。
- 安全默认：设置 `NOVEL_AUTH_TOKEN` 后 `/api/*` 需 `Authorization: Bearer <token>`，比较为常数时间（SHA-256 摘要 + `hmac.compare_digest`）并对失败做进程内指数退避节流；所有响应带 `Cache-Control: no-store`、`X-Content-Type-Options: nosniff`、`X-Frame-Options: DENY`；请求体上限 1MB（超限 413 `payload_too_large`，负长度拒绝）；静态文件已做路径穿越/空字节防护。健康检查与前端页面保持公开。

完整配置见 `.env.example`；`DEEPSEEK_BASE_URL` 和 `DEEPSEEK_API_KEY` 必须由部署环境注入，代码不内置供应商地址或密钥。缺少配置时服务不会伪造模型结果，生成任务会安全失败并记录配置错误。发布是半自动的：审查通过后导出 TXT/Markdown/JSON/DOCX，用户在目标平台手动发布，再回系统确认；没有番茄自动点击、登录或验证码绕过。

默认启用 `NOVEL_AUTO_EXPORT_TXT=true`：章节完整生成并通过自动审查后，会同步写入 `data/exports/<小说名>_<卷名>_第0001章_<章节标题>.txt`。文件名会清理跨平台不安全字符；该文件是待审核草稿，不会把章节改成已发布或正式导出状态，人工发布流程保持不变。

导出支持四种格式：TXT / Markdown / JSON / **DOCX**。DOCX 用标准库直接生成最小合法 OOXML 包（不引入 python-docx），标题与章节名加粗、正文按空行分段，Office Word/WPS 可直接打开；API 与导出按钮同样支持 `format=docx`。

## 多智能体流水线与断点恢复

- **写作流程（责任链）**：`大纲(outline) → 正文(chapter) → 润色(polish)`。通过环境变量 `NOVEL_AGENT_STAGES`（逗号分隔）或前端「生成参数 → 写作流程」选择；留空为传统的单次合成模式（向后兼容，API 不传 `config` 时行为不变）。每章可配置目标字数 `config.targetWords`（覆盖圣经缺省并传给 Reviewer）。
- **Prompt 治理**：所有提示词提取到仓库 `prompts/*.md`（`system.md` / `request.md` / `repair.md` / `outline.md` / `chapter.md` / `polish.md`），由 `novel_agent/prompts.py` 的 `PromptManager` 统一加载、渲染与占位符校验，并写入 `usage.prompt_version`（`novel-writer@2`）以追踪每次调用使用的提示词版本。
- **检查点与恢复**：每完成一个 Agent 节点即在一个事务中把上下文/记忆写入 `agent_runs` / `agent_stages` / `checkpoints` 三张表。worker/服务重启后，`NOVEL_AUTO_RESUME=true`（默认）时，中断的任务会从**最后一个未完成节点继续**，已完成节点不再重复调用模型（不重复计费）。数据存取统一走 `novel_agent/checkpoints.py` 的 `CheckpointRepository`（DAO，复用每线程独立连接 + `BEGIN IMMEDIATE` 的线程安全模型）。
- 诚实边界：单次模型调用内部不做 token 级续接（生成式模型无法安全续写）；安全恢复粒度是一个已完成并落检查点的节点。
- 新增 API：`GET /api/novels/<id>/runs/latest` 返回最近一次 run 及其阶段状态；`POST /api/novels/<id>/chapters/generate` 与 `continue` 可携带 `config`（`stages` / `targetWords` / `autoExportTxt`）。

## 异步模型调用与实时流式

自 v0.3.0 起运行时依赖为 `httpx` + `tenacity`（见 `pyproject.toml`；`pip install -e .` 自动安装）。

- **全异步 DeepSeek 客户端** `novel_agent/llm.py`：`AsyncLLMClient` 基于 httpx 流式接收，错误分类与同步客户端完全一致；`tenacity` 负责重试策略（指数退避、上限 30s，HTTP 429 优先尊重 `Retry-After`），`NOVEL_MAX_RETRIES` 控制尝试次数；`stream()` 逐块产出 `delta`。
- **Worker 流式处理**：worker 主循环改用 `asyncio.run(service.process_stream(job))`，每个 token/阶段事件实时写入 `events` 表（`llm.delta` 节流合并、`llm.text` 携带已验证正文、`agent.stage/run`、`checkpoint.saved`、`chapter.ready`）。
- **SSE 事件接口**：`GET /api/events?novel_id=<id>&since=<cursor>` 以 `text/event-stream` 保持长连接推送（每 0.4s 轮询 events 表、15s 心跳注释），浏览器断线后用 `since` 续读不丢帧；鉴权与 `/api` 一致，通过 fetch 携带 `Authorization`。
- **前端逐字打印**：控制台在正文阶段完成后用打字机效果逐字揭示 `llm.text` 内容；阶段切换、token 接收计数由事件流实时驱动（失败自动回退轮询）。

## 无 Key 演示：录制与回放（demo）

演示链路复用**真实生产路径**（`service.process_stream` 流水线 + `events` 表 + SSE
端点 + 前端打字机），只是把模型换成一个**确定性回放器**，全程不需要
`DEEPSEEK_API_KEY` 也不联网：

```bash
python -m novel_agent.demo replay                # 内置演示录制，headless
python -m novel_agent.demo replay --port 8890    # 同时起真实 HTTP/SSE，浏览器观看
python -m novel_agent.demo replay --replay data/replays/c1.json --port 8890
```

- `record`：带真实 Key 跑一章（`--novel-id` + `--chapter` + `--stages`），把每次
  模型调用的 `(system, user, text, usage)` 与小说快照存成 replay JSON。
- `replay`：按调用顺序回放。`record` 生成的文件默认**严格校验**当前提示词与录制的
  一致（防止旧录制静默答非所问），场景不符时可用 `--loose` 仅按顺序回放；内置演示
  固定场景无需校验。
- 回放走完整多智能体流程（outline → chapter → polish）、断点检查点、`llm.delta`
  事件与审查门禁；章节完成后打印事件时间线并在终端逐字揭示正文。
- 不插桩生产代码：`novel_agent/replay.py` 与 `novel_agent/demo.py` 都是独立新增，
  worker/server 与真实客户端不受影响。测试：`tests/test_demo.py`（8 例）。

## 验证

```bash
make lint     # compileall + git diff --check
make test     # python3 -m unittest discover -s tests -v
make build
```

测试全部使用随机空闲端口、隔离的 `NOVEL_DATA_DIR` 与指向不存在文件的 `NOVEL_ENV_FILE`，因此即使本机 8787 已运行生产 server/worker，测试也不会与之冲突。

## API

已实现 `POST/GET /api/novels`、StoryBible 编辑、章节生成/列表、job 查询/取消、review、approve、导出、人工发布确认、pause/continue 和指定章节/连续 N 章任务创建；错误统一为 `{code,message,details}`。

章节列表支持服务端投影与检索（避免把整本书下发给浏览器再本地过滤）：

- `GET /api/novels/<id>/chapters` 返回全部章节（含正文）。
- `GET /api/novels/<id>/chapters?light=1` 省略正文、改用 `content_length` 表示正文字数——
  控制台轮询用这一档，正文按需用 `GET /api/chapters/<id>` 单独取回。
- `?q=<关键词>` 由 SQLite 在服务端按「标题 / 章节号 / 状态 / 目标」检索整本小说。
- `?limit=N`（≤5000）限制返回条数；不带 `q` 时返回**最近 N 章**（升序）。

人工审查/发布的状态门禁全部在服务端执行（前端隐藏按钮只是附加保护）：

- `POST /api/chapters/<id>/review` 会把数据库行（`goal`/`state_changes`…）投影成模型输出契约
  （`chapterGoal`/`stateChanges`…）后再审查，并把**本章自身与后续章节**排除在「最近章节」窗口外，
  因此不会把自己正文判成 `recent_chapter_overlap`。
- `POST /api/chapters/<id>/approve` 仅在该章最近一次审查 `passed=true` 且无 `blockingIssues` 时通过（否则 409）。
- 终态章节（`PUBLISHED_MANUALLY` / `CANCELLED`）的正文被冻结：`PATCH /api/chapters/<id>` 与
  `POST /api/chapters/<id>/rollback` 返回 409 `chapter_is_terminal`，保证「发布即不可回退」的审计不变量。
- 已导出章节再次审查不会被打回（保留 `EXPORTED`），否则会破坏「先导出再人工发布」的门禁。

`POST /api/chapters/<id>/rewrite` 提供「删除并重写」：仅允许针对当前最新、尚未人工发布的草稿章，在一个事务中清除该章正文/审查/草稿历史及其任务和用量后，按 StoryBible 大纲重新排队生成同一章号（串联式覆盖重写，保证不与后续章节冲突）。

`POST /api/novels/<id>/chapters/generate` 与 `POST /api/novels/<id>/continue` 遵循串行确认制：当「下一章」编号已存在一份已完成但尚未人工发布的草稿（待人工批准/已批准待导出/已导出）时，返回 `409 conflict / confirm_previous_chapter_first`（`details` 含 `chapterNumber`），提示先完成该章的「批准 → 导出 → 已人工发布」确认。这样避免把旧的成功任务误报为「正在队列中」而永不生成。

## 前端

前端已重构为三文件：`static/index.html`（骨架）+ `static/app.css`（设计令牌与样式）+ `static/app.js`（逻辑）。采用宽屏双栏布局：

- **左侧栏**：小说选择、生成参数（写作流程单选 + 目标字数）、**Agent 状态时间线**（大纲设计 → 正文撰写 → 润色，彩色 Badge + 脉冲动画 + 当前节点 Spinner）、章节列表、任务/用量看板、StoryBible 编辑。
- **右侧主区**：选中章节的沉浸式阅读排版（衬线字体、行高 1.95、居中 44em 阅读栏、段落缩进与间距），正文/梗概/审查结果分区展示，章节操作（编辑/审查/批准/导出/发布/删除重写/重试）内联呈现。
- **状态指示**：顶部细进度条 + 当前节点 Spinner，Agent 调用模型期间由轮询驱动；断网/缺 Key/401 等以顶部横幅醒目提示修复步骤。深浅色主题跟随系统并可手动切换。
- **实时流式**：通过 `GET /api/events` 的 SSE 长连接，生成期间左侧 Agent 时间线实时流转，右侧在正文阶段完成后用打字机逐字揭示内容（闪烁光标）；连接断开后用事件游标自动续读，失败则回退轮询。
- **草稿版本历史 / 差异 / 回滚**：每版草稿（含审查结果）一直留档在 `chapter_drafts`，阅读区「📑 历史」可预览任意版本、与当前稿逐行对比（红/绿差异）并回滚（生成新版本、需重新审查；已发布章节锁定）。
- **生成过程回放**：「🎞 回放」按 SSE 原序重现该章最近一次生成的 `agent.stage / llm.delta / llm.text / chapter.ready` 事件，含正文打字机效果（数据来自 `GET /api/novels/<id>/chapters/<n>/timeline`）。

后端对应新增：`GET /api/chapters/<id>/history`、`POST /api/chapters/<id>/rollback`、`GET /api/novels/<id>/chapters/<n>/timeline`（均受鉴权保护）。CI：`.github/workflows/tests.yml` 在 ubuntu/windows × Python 3.12/3.13 矩阵跑全量单测。

后端接口未变（新增的 `runs/latest` 与 `config` 为可选增量）；若生成失败，先在页面顶部横幅确认是否为「缺少 DeepSeek API Key」后再排查模型/网络问题。

## 近期增强

- **章节导航**：侧栏章节列表支持按标题/序号/状态搜索过滤；阅读区头部提供 上一章/下一章
  与进度指示；页面会记住上次阅读的小说与章节，刷新后自动回到原处。
- **整本导出**：有已导出/已发布章节的小说，可在顶部点「📚 全本 TXT/MD」一键导出按章排序的
  合集文件（`POST /api/novels/<id>/export-book`）。
- **Story Bible 字段表单**：Story Bible 面板可切到「🧾 表单」逐字段编辑（标量输入框、
  对象/数组 JSON 编辑、删除与新增字段），保存与文本 JSON 模式共用同一接口与版本递增。
- **长文渲染**：超长章节正文先渲染前 250 段并可按需展开，避免整段超长页面卡顿；章节列表
  超过 400 章时只渲染最近 400 章（可用搜索定位更早章节）。
- **任务/审计查询**：`GET /api/novels/<id>/jobs` 支持 `?status=&limit=`，
  `GET /api/ops/audit` 支持 `?action=&novel_id=` 过滤与分页。

## 真实性与性能审计（2026-09-10）

一轮针对「看起来能跑」与「真实可用」差距的审计，修复项：

- **手动重新审查曾必然失败**：`POST /api/chapters/<id>/review` 过去把数据库行直接喂给
  `reviewer.review`，而审查器读的是模型输出契约（`chapterGoal`/`stateChanges`…），于是每次都返回
  伪造的 `missing_chapter_goal` 阻断项并把章节打成 `FAILED`，使「编辑正文 → 重新审查 → 批准 → 导出」
  整条链路死锁。现在由 `reviewer.chapter_as_output()` 统一投影，并新增回归测试。
- **已发布正文可被静默改写**：`PATCH /api/chapters/<id>` 此前没有终态校验，能覆盖 `PUBLISHED_MANUALLY`
  章节的正文并把它降级成 `REVIEWING`（破坏审计不变量）。现在终态章节返回 409 `chapter_is_terminal`。
- **批准缺少服务端门禁**：`approve` 过去只校验 `blockingIssues`，未审查的草稿也能批准；现在要求
  `review.passed=true`。
- **非法生成参数会留下孤儿任务**：`config` 校验现移到 `create_job` 之前，400 不再产生 PENDING 任务。
- **导出失败不再"假装成功"**：I/O 失败会写入 `export_jobs.status='FAILED'` 并返回 500/`export_failed`，
  不再把行留在 `PENDING` 且不置章节为 `EXPORTED`。
- **查询与轮询成本**：`store.recent`（曾把整本书读进内存再切片）、`store.chapters`、`store.published_chapters`、
  `store.novels` 全部改为单条有界 SQL；`usage_series` 改为可走索引的 `created_at >= ?`；
  `events(created_at)` 新增索引（保留期清理不再全表扫描）。
- **首屏/轮询体积**：章节列表默认走 `?light=1&limit=500`，正文按需取回；超过窗口时搜索自动切到服务端
  `?q=` 全库检索（含分页上限），不再把整本小说下载到浏览器后 `array.filter`。
- **UI 数据字段错配**：阅读区「本章梗概」读的是 `chapterGoal`/`nextChapterHook`，而持久化行为
  `goal`/`hook`，导致「目标 / 下章钩子」从未显示；现已兼容两种键。
- **状态机常量落地**：`models.ACTIVE`/`TERMINAL` 不再是无引用常量，分别用于取消任务与终态冻结判定。
- **死代码清理**：移除未引用的 `demo.DEFAULT_PORT`、`events.latest_id`、`worker._process_one`；
  `AgentContext.load_memory` 由 `stages.py` 复用（去掉两处重复的 JSON 解析）。
- **新增测试**：`tests/test_review_flow.py`（审查/批准/导出/冻结/轻量列表/服务端检索，22 例）与
  `tests/test_e2e_persistence.py`（真实子进程 + 重启后持久化验证，1 例）。
