# Novel Agent

一个由项目自身服务端、SQLite 数据库和可恢复 Worker 驱动的长篇小说创作智能体。浏览器只调用本地 API；DeepSeek key 只从服务端环境读取。

## 运行

server 与 worker 启动时会自动加载 `.env`（查找顺序：`NOVEL_ENV_FILE` → 当前目录 `.env` → 仓库 `.env`），无需手动 `source`；进程已有的环境变量始终优先于文件，文件不存在也不报错。配置解析失败会在启动时抛出 `ConfigError`，带病的配置不会被静默忽略。

方式 A：无需安装，直接以模块运行：

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
- 优雅退出：server 与 worker 收到 `SIGTERM`/`SIGINT` 后停止接收新请求/任务，收尾后以退出码 0 结束，适合被 systemd/launchd/容器托管。
- 结构化日志：`NOVEL_LOG_FORMAT=text|json`（JSON 每行一个对象并合并 job/chapter 等字段），`NOVEL_LOG_LEVEL` 控制级别；每条 HTTP 请求记录 method/path/status/duration_ms。
- 运维指标（与 `/api` 一样受鉴权保护）：`GET /api/ops/metrics` 聚合快照、`GET /api/ops/usage?days=N`（≤90）按日零填充趋势、`GET /api/ops/audit?limit=N`（≤1000）审计轨迹（新在前）；写操作自动落审计：建书/改 Bible/取消任务/重写章节/导出/人工发布。
- DB 维护 CLI：`python3 -m novel_agent.ops stats --db data/novel.sqlite3`（打印指标+日趋势+磁盘占用）、`backup --db … --out … [--force]`（SQLite 在线备份，可边运行边备份）、`vacuum --db …`（WAL checkpoint + VACUUM）。也可用 `novel-agent-ops` 等 console script。
- 安全默认：设置 `NOVEL_AUTH_TOKEN` 后 `/api/*` 需 `Authorization: Bearer <token>`，比较为常数时间（SHA-256 摘要 + `hmac.compare_digest`）并对失败做进程内指数退避节流；所有响应带 `Cache-Control: no-store`、`X-Content-Type-Options: nosniff`、`X-Frame-Options: DENY`；请求体上限 1MB（超限 413 `payload_too_large`，负长度拒绝）；静态文件已做路径穿越/空字节防护。健康检查与前端页面保持公开。

完整配置见 `.env.example`；`DEEPSEEK_BASE_URL` 和 `DEEPSEEK_API_KEY` 必须由部署环境注入，代码不内置供应商地址或密钥。缺少配置时服务不会伪造模型结果，生成任务会安全失败并记录配置错误。发布是半自动的：审查通过后导出 TXT/Markdown/JSON，用户在目标平台手动发布，再回系统确认；没有番茄自动点击、登录或验证码绕过。

默认启用 `NOVEL_AUTO_EXPORT_TXT=true`：章节完整生成并通过自动审查后，会同步写入 `data/exports/<小说名>_<卷名>_第0001章_<章节标题>.txt`。文件名会清理跨平台不安全字符；该文件是待审核草稿，不会把章节改成已发布或正式导出状态，人工发布流程保持不变。

DOCX 未实现，因为审计发现项目原本没有文档生成能力；可在未来加入独立适配器，不影响现有导出格式。

## 验证

```bash
make lint     # compileall + git diff --check
make test     # python3 -m unittest discover -s tests -v
make build
```

测试全部使用随机空闲端口、隔离的 `NOVEL_DATA_DIR` 与指向不存在文件的 `NOVEL_ENV_FILE`，因此即使本机 8787 已运行生产 server/worker，测试也不会与之冲突。

## API

已实现 `POST/GET /api/novels`、StoryBible 编辑、章节生成/列表、job 查询/取消、review、approve、导出、人工发布确认、pause/continue 和指定章节/连续 N 章任务创建；错误统一为 `{code,message,details}`。

`POST /api/chapters/<id>/rewrite` 提供「删除并重写」：仅允许针对当前最新、尚未人工发布的草稿章，在一个事务中清除该章正文/审查/草稿历史及其任务和用量后，按 StoryBible 大纲重新排队生成同一章号（串联式覆盖重写，保证不与后续章节冲突）。

`POST /api/novels/<id>/chapters/generate` 与 `POST /api/novels/<id>/continue` 遵循串行确认制：当「下一章」编号已存在一份已完成但尚未人工发布的草稿（待人工批准/已批准待导出/已导出）时，返回 `409 conflict / confirm_previous_chapter_first`（`details` 含 `chapterNumber`），提示先完成该章的「批准 → 导出 → 已人工发布」确认。这样避免把旧的成功任务误报为「正在队列中」而永不生成。

## 前端

`static/index.html` 为单文件控制台：状态徽章、按状态着色的章节卡片、Job/用量看板、自动轮询刷新、问题横幅（如 `DEEPSEEK_API_KEY` 缺失会醒目提示并给出补齐 `.env` 后重启 server/worker 的修复步骤）、鉴权与深浅色主题切换。后端未改动；若生成失败，先在页面顶部横幅确认是否为「缺少 DeepSeek API Key」后再排查模型/网络问题。
