import json
from pathlib import Path


def export_chapter(chapter, novel, fmt, directory):
    if not chapter['review'].get('passed') or chapter['review'].get('blockingIssues'): raise ValueError('chapter review prevents export')
    if fmt not in ('txt','md','json'): raise ValueError('unsupported export format')
    directory=Path(directory); directory.mkdir(parents=True,exist_ok=True)
    meta={'novel':novel.get('title',''),'volume':novel.get('volume',''),'chapterNumber':chapter.get('number'),'title':chapter.get('title',''),'content':chapter.get('content',''),'summary':chapter.get('summary',''),'characters':chapter.get('characters',[]),'events':chapter.get('events',[]),'foreshadowingAdded':chapter.get('foreshadowing_added',[]),'foreshadowingResolved':chapter.get('foreshadowing_resolved',[]),'review':chapter.get('review',{}),'model':chapter.get('model',''),'generatedAt':chapter.get('generated_at',''),'storyBibleVersion':novel.get('story_bible_version',0)}
    if fmt=='json': body=json.dumps(meta,ensure_ascii=False,indent=2)
    elif fmt=='md': body=f"# {novel['title']}\n\n## {chapter['number']}. {chapter['title']}\n\n{chapter['content']}\n"
    else: body=f"{novel['title']}\n{novel.get('volume','')} 第{chapter['number']}章 {chapter['title']}\n\n{chapter['content']}\n"
    path=directory/f"{novel['id']}-{chapter['number']}.{fmt}"; path.write_text(body,encoding='utf-8'); return path
