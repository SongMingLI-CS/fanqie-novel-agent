# 半自动发布流程与安全边界

## 发布器

- `DryRunPublisher`：只校验并记录将要发布的摘要，不产生外部副作用。
- `LocalFilePublisher`：将已批准章节写入配置目录，使用原子临时文件与稳定命名，供人工上传。
- `ExternalPublisher`：仅保留未来由正式平台 API 实现的接口；当前版本不调用它，不使用浏览器点击、登录、验证码或平台自动化。

代码中的 `publishers.py` 提供 `DryRunPublisher`、`LocalFilePublisher` 和 `ExternalPublisher` 契约。前两者只产生 dry-run 结果或本地文件；`ExternalPublisher` 没有默认实现，避免误接入外部平台。

## 发布前门禁

服务端必须确认：任务为 `DRAFT_READY` 或 `WAITING_APPROVAL`；审查无阻断问题；章节内容完整；章节尚未导出；导出幂等键未成功使用；审核策略允许。`NOVEL_PUBLISH_ENABLED` 保持默认 `false`，本版本只允许导出和人工确认，不允许自动发布。

## 幂等与失败

每次导出使用 `novelId:chapterNumber:draftVersion:format` 派生的持久化幂等键，重复请求返回已有文件。人工发布确认保存平台、章节号、可选外链、发布时间、操作人和备注，并将状态改为 `PUBLISHED_MANUALLY`。

## 人工流程

Reviewer 通过后章节进入 `WAITING_APPROVAL`。用户在控制台查看正文与结构化审查结果，批准后才可导出；用户在番茄作家助手等平台完成手工发布，再点击“已人工发布”确认。连续生成 N 章在每章生成后暂停，只有用户确认后才创建下一章任务；暂停小说会阻止新任务，但不删除历史草稿。
