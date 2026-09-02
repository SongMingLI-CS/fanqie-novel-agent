# Novel Agent

一个由项目自身服务端、SQLite 数据库和可恢复 Worker 驱动的长篇小说创作智能体。浏览器只调用本地 API；DeepSeek key 只从服务端环境读取。

## 运行

```bash
python3 -m novel_agent.server
# 另一个终端启动可恢复 Worker
python3 -m novel_agent.worker
```

打开 <http://127.0.0.1:8787>。首次创建小说时会读取 `.agents/skills/novel-writer/references/` 下的四份 Skill 参考文件并保存初始 StoryBible 版本。

完整配置见 `.env.example`；`DEEPSEEK_BASE_URL` 和 `DEEPSEEK_API_KEY` 必须由部署环境注入，代码不内置供应商地址或密钥。缺少配置时服务不会伪造模型结果，生成任务会安全失败并记录配置错误。发布是半自动的：审查通过后导出 TXT/Markdown/JSON，用户在目标平台手动发布，再回系统确认；没有番茄自动点击、登录或验证码绕过。

DOCX 未实现，因为审计发现项目原本没有文档生成能力；可在未来加入独立适配器，不影响现有导出格式。

## 验证

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall novel_agent tests
```

## API

已实现 `POST/GET /api/novels`、StoryBible 编辑、章节生成/列表、job 查询/取消、review、approve、导出、人工发布确认、pause/continue 和指定章节/连续 N 章任务创建；错误统一为 `{code,message,details}`。
