# Chapter JSON Template

模型必须返回单个 JSON 对象，不得包裹 Markdown 代码围栏：

```json
{
  "chapterNumber": 1,
  "title": "章节标题",
  "chapterGoal": "本章目标",
  "beats": [],
  "summary": "本章剧情摘要",
  "content": "章节正文",
  "charactersUsed": [],
  "eventsIntroduced": [],
  "foreshadowingAdded": [],
  "foreshadowingResolved": [],
  "stateChanges": [],
  "nextChapterHook": "",
  "warnings": []
}
```

`beats`、人物、事件、伏笔和状态变更使用结构化对象；正文必须有完整段落。额外字段不得替代必需字段。状态变更先作为 `proposed-story-state` 保存，不能直接改变 Story Bible。
