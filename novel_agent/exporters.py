import json
from pathlib import Path


def export_chapter(chapter, novel, fmt, directory):
    if chapter['review'].get('blockingIssues'): raise ValueError('blockingIssues prevent export')
    if fmt not in ('txt','md','json'): raise ValueError('unsupported export format')
    directory=Path(directory); directory.mkdir(parents=True,exist_ok=True)
    meta={'novel':novel['title'],'volume':novel.get('volume',''),'chapterNumber':chapter['number'],'title':chapter['title'],'content':chapter['content'],'summary':chapter['summary'],'characters':chapter['characters'],'events':chapter['events'],'foreshadowingAdded':chapter['foreshadowing_added'],'foreshadowingResolved':chapter['foreshadowing_resolved'],'review':chapter['review'],'model':chapter['model'],'generatedAt':chapter['generated_at'],'storyBibleVersion':novel['story_bible_version']}
    if fmt=='json': body=json.dumps(meta,ensure_ascii=False,indent=2)
    elif fmt=='md': body=f"# {novel['title']}\n\n## {chapter['number']}. {chapter['title']}\n\n{chapter['content']}\n"
    else: body=f"{novel['title']}\n{novel.get('volume','')} 第{chapter['number']}章 {chapter['title']}\n\n{chapter['content']}\n"
    path=directory/f"{novel['id']}-{chapter['number']}.{fmt}"; path.write_text(body,encoding='utf-8'); return path
