# 发布流程与安全边界

## 发布器

- `DryRunPublisher`：只校验并记录将要发布的摘要，不产生外部副作用。
- `LocalFilePublisher`：将已批准章节写入配置目录，使用原子临时文件与稳定命名，供人工上传。
- `ExternalPublisher`：定义 `validate`, `publish`, `confirm` 契约；没有正式平台 API 时不得用浏览器点击、绕过验证码、登录验证或平台限制。

## 发布前门禁

服务端必须确认：任务为 `DRAFT_READY` 或 `WAITING_APPROVAL`；审查无阻断问题；章节内容完整；章节尚未发布；目标平台配置存在；幂等键未成功使用；`NOVEL_PUBLISH_ENABLED=true`；若 `NOVEL_REQUIRE_REVIEW=true`，已有明确人工批准。默认使用 `DryRunPublisher`，并默认需要审核。

## 幂等与失败

每次发布使用 `novelId:chapterNumber:draftVersion:publisher` 派生的持久化幂等键。请求超时后先 `confirm` 再决定重试，避免重复发布；未知状态进入可恢复状态，不直接标记成功。可重试错误使用有限指数退避，永久错误进入 `FAILED`/死信并保留安全错误码。

## 人工流程

Reviewer 通过后章节进入 `WAITING_APPROVAL`。用户在控制台查看正文与结构化审查结果，批准后才可触发发布。自动发布只有在环境开关和审核策略同时允许时可用；暂停小说会阻止新任务和发布，但不删除历史草稿。
