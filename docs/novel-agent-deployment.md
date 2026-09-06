# 部署与恢复说明

## 进程

服务端和 Worker 都由项目自身运行，不依赖 Codex。建议使用一个受保护的本机/内网目录，并以非 root 用户运行。两者启动时都会自动加载 `.env`（`NOVEL_ENV_FILE` → 当前目录 `.env` → 仓库 `.env`），进程已有环境变量优先于文件；配置解析失败会在启动时抛出 `ConfigError` 立即退出。

```bash
# 无需安装，直接以模块运行
python3 -m novel_agent.server
python3 -m novel_agent.worker

# 或可编辑安装后使用 console script（python3 -m pip install -e .）
novel-agent-server
novel-agent-worker
novel-agent-ops
```

监听地址/端口由 `NOVEL_HOST`/`NOVEL_PORT` 控制（默认 `127.0.0.1:8787`；`NOVEL_PORT=0` 使用随机空闲端口并在 stdout 打印 `NOVEL_AGENT_LISTENING host:port`，便于编排层动态发现）。两个进程收到 `SIGTERM`/`SIGINT` 后优雅退出（停止接收新请求/任务、收尾后以退出码 0 结束），适合被 systemd/launchd/容器托管；Windows 控制台下同样注册 `SIGBREAK`，支持 `CTRL+BREAK` 优雅停止。

健康检查无需鉴权：`GET /healthz` 为存活探测恒 200；`GET /readyz` 为就绪探测，会额外对 SQLite 执行 `SELECT 1`，不可用时返回 503 `store_unavailable`。可在反向代理/负载均衡处配置，避免把流量打到未就绪实例。

生产环境必须设置 `NOVEL_AUTH_TOKEN`，通过反向代理或服务管理器注入环境变量；不要把 `.env` 提交到 Git。`DEEPSEEK_API_KEY` 只注入服务端和 Worker 进程。默认 `NOVEL_PUBLISH_ENABLED=false`、`NOVEL_REQUIRE_REVIEW=true`，系统只导出并记录人工发布确认。

DeepSeek 请求使用流式响应；连接超时默认为 10 秒，整体请求超时默认为 180 秒，最多重试 2 次。可通过 `DEEPSEEK_CONNECT_TIMEOUT`、`NOVEL_REQUEST_TIMEOUT` 和 `NOVEL_MAX_RETRIES` 调整。日志只记录脱敏的 DNS/TLS/HTTP 状态、阶段和耗时，不记录请求体或密钥。

模型只从 `DEEPSEEK_MODEL` 读取。当前验证配置为 `deepseek-v4-flash`；结构化长文本默认设置 `DEEPSEEK_THINKING=disabled`，避免推理 Token 挤占 JSON 正文预算。需要推理模式时可显式设置 `enabled`，并通过 `DEEPSEEK_REASONING_EFFORT=low|high|max` 调整，但上线前必须重新执行最小认证和长文本流测试。

## 数据与备份

`NOVEL_DATA_DIR` 下的 `novel.sqlite3` 是持久化状态，`exports/` 是导出稿。备份前暂停 Worker，然后复制 SQLite 文件；启用 WAL 时需同时复制 `novel.sqlite3-wal` 与 `novel.sqlite3-shm`，或使用 SQLite 在线备份工具。恢复时停止服务，恢复同一目录，再启动服务和 Worker。不得删除旧的 StoryBible 版本或发布记录。

项目自带零依赖运维 CLI `novel-agent-ops`（或 `python3 -m novel_agent.ops`），基于 SQLite 在线备份 API，**可在 server/worker 持续写入时运行**：

```bash
# 运维快照：指标 + 按日趋势 + 磁盘占用（JSON，便于 cron/看板采集）
novel-agent-ops stats --db data/novel.sqlite3 --days 14
# 在线一致性备份（已存在时报错，--force 才覆盖）
novel-agent-ops backup --db data/novel.sqlite3 --out backups/novel-$(date +%F).sqlite3
# WAL checkpoint(TRUNCATE) + VACUUM 回收空间
novel-agent-ops vacuum --db data/novel.sqlite3
```

备份文件可直接用 `Store`/sqlite 打开校验（`stats` 前先 `Store(...)` 会重建 schema，仅作只读快照核对）。

若运行环境的 Python OpenSSL 找不到系统 CA，设置 `DEEPSEEK_CA_BUNDLE` 指向受信任的 CA bundle；不要通过关闭证书校验解决 TLS 错误。

## 运维检查

- Worker 重启后会回收过期租约；检查 `jobs.status`、`attempts` 和 `error`。
- Worker 对任务处理过程中未预期的代码级异常会立即调用 `fail_job`（按 `NOVEL_MAX_JOB_ATTEMPTS` 退避重试，用尽后置为 `FAILED`），不会把任务留在 `RUNNING` 直到租约过期再无限重试；错误前缀为 `worker_crash:<类型>`。
- `FAILED` 任务可由控制台重新生成；`CANCELLED` 不会自动重跑。
- 发布前检查 `ReviewResult.passed`、`blockingIssues`、`EXPORTED` 和人工发布记录。
- 指标与审计（受鉴权保护）：`GET /api/ops/metrics` 聚合（novels/chapters/jobs/usage/byModel/exports/publishes/auditEvents）；`GET /api/ops/usage?days=N` 按日请求/成功/失败/Token；`GET /api/ops/audit?limit=N` 看谁在何时改了哪本小说（建书/改 Bible/取消/重写/导出/发布）。写操作自动追加审计，审计表只增不改。
- 日志级别/格式由 `NOVEL_LOG_LEVEL`、`NOVEL_LOG_FORMAT` 控制；`json` 格式每行一个对象并合并 job/chapter 等字段，便于采集；日志只记录模型、提示词版本、Token、耗时、HTTP method/path/status/duration_ms 和安全错误码，不记录 API key、鉴权头或完整长篇大纲。
- 鉴权：设置 `NOVEL_AUTH_TOKEN` 后 `/api/*`（含 `/api/ops/*`）需 `Authorization: Bearer <token>`；比较为常数时间并带进程内指数退避（同一客户端失败第 5 次起延迟，封顶 2 秒），`/healthz`、`/readyz` 与前端页面公开。所有响应带 `Cache-Control: no-store`、`X-Content-Type-Options: nosniff`、`X-Frame-Options: DENY`；请求体上限 1MB（超限 413）。
- 多实例部署前应迁移到具备可靠行锁/队列的数据库；当前 SQLite 方案适合单机或低并发。

## 真实外部平台

当前没有番茄账号接入、浏览器点击、登录、验证码或自动发布适配器。用户应在目标平台手工发布后调用人工确认接口，外部链接可以为空。
