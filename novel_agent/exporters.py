import json
import os
import re
import uuid
from pathlib import Path


def safe_filename_component(value, fallback, limit=60):
    value=re.sub(r'[<>:"：/\\|?*\x00-\x1f]+','_',str(value or '').strip())
    value=re.sub(r'\s+',' ',value).strip(' ._')
    return (value or fallback)[:limit].rstrip(' .')


def export_filename(chapter, novel, fmt):
    novel_title=safe_filename_component(novel.get('title'),'未命名小说')
    volume=safe_filename_component(novel.get('volume'),'') if novel.get('volume') else ''
    chapter_title=safe_filename_component(chapter.get('title'),'未命名章节')
    number=max(0,int(chapter.get('number') or 0))
    parts=[novel_title]
    if volume: parts.append(volume)
    parts.extend((f'第{number:04d}章',chapter_title))
    return '_'.join(parts)+f'.{fmt}'


def export_chapter(chapter, novel, fmt, directory):
    if not chapter['review'].get('passed') or chapter['review'].get('blockingIssues'): raise ValueError('chapter review prevents export')
    if fmt not in ('txt','md','json'): raise ValueError('unsupported export format')
    directory=Path(directory); directory.mkdir(parents=True,exist_ok=True)
    meta={'novel':novel.get('title',''),'volume':novel.get('volume',''),'chapterNumber':chapter.get('number'),'title':chapter.get('title',''),'content':chapter.get('content',''),'summary':chapter.get('summary',''),'characters':chapter.get('characters',[]),'events':chapter.get('events',[]),'foreshadowingAdded':chapter.get('foreshadowing_added',[]),'foreshadowingResolved':chapter.get('foreshadowing_resolved',[]),'review':chapter.get('review',{}),'model':chapter.get('model',''),'generatedAt':chapter.get('generated_at',''),'storyBibleVersion':novel.get('story_bible_version',0)}
    if fmt=='json': body=json.dumps(meta,ensure_ascii=False,indent=2)
    elif fmt=='md': body=f"# {novel['title']}\n\n## {chapter['number']}. {chapter['title']}\n\n{chapter['content']}\n"
    else: body=f"{novel['title']}\n{novel.get('volume','')} 第{chapter['number']}章 {chapter['title']}\n\n{chapter['content']}\n"
    path=directory/export_filename(chapter,novel,fmt)
    temporary=directory/f".{path.name}.{uuid.uuid4().hex}.tmp"
    try:
        temporary.write_text(body,encoding='utf-8')
        os.replace(temporary,path)
    finally:
        temporary.unlink(missing_ok=True)
    return path
