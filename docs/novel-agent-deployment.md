# 部署与恢复说明

## 进程

服务端和 Worker 都由项目自身运行，不依赖 Codex。建议使用一个受保护的本机/内网目录，并以非 root 用户运行：

```bash
python3 -m novel_agent.server
python3 -m novel_agent.worker
```

生产环境必须设置 `NOVEL_AUTH_TOKEN`，通过反向代理或服务管理器注入环境变量；不要把 `.env` 提交到 Git。`DEEPSEEK_API_KEY` 只注入服务端和 Worker 进程。默认 `NOVEL_PUBLISH_ENABLED=false`、`NOVEL_REQUIRE_REVIEW=true`，系统只导出并记录人工发布确认。

## 数据与备份

`NOVEL_DATA_DIR` 下的 `novel.sqlite3` 是持久化状态，`exports/` 是导出稿。备份前暂停 Worker，然后复制 SQLite 文件；启用 WAL 时需同时复制 `novel.sqlite3-wal` 与 `novel.sqlite3-shm`，或使用 SQLite 在线备份工具。恢复时停止服务，恢复同一目录，再启动服务和 Worker。不得删除旧的 StoryBible 版本或发布记录。

## 运维检查

- Worker 重启后会回收过期租约；检查 `jobs.status`、`attempts` 和 `error`。
- `FAILED` 任务可由控制台重新生成；`CANCELLED` 不会自动重跑。
- 发布前检查 `ReviewResult.passed`、`blockingIssues`、`EXPORTED` 和人工发布记录。
- 日志只记录模型、提示词版本、Token、耗时、状态和安全错误码，不记录 API key 或完整长篇大纲。
- 多实例部署前应迁移到具备可靠行锁/队列的数据库；当前 SQLite 方案适合单机或低并发。

## 真实外部平台

当前没有番茄账号接入、浏览器点击、登录、验证码或自动发布适配器。用户应在目标平台手工发布后调用人工确认接口，外部链接可以为空。
