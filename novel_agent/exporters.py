import io
import json
import os
import re
import uuid
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

# Supported export formats. ``docx`` is emitted as a minimal OOXML package
# (built with the standard library only), so no python-docx dependency.
EXPORT_FORMATS = ("txt", "md", "json", "docx")

DOCX_CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="xml" ContentType="application/xml"/>
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
</Types>"""

DOCX_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>"""

DOCX_DOCUMENT_OPEN = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
    "<w:body>"
)


def _docx_paragraph(text=None, bold=False, size_half_points=None):
    """One ``<w:p>`` element. ``text=None`` yields an empty paragraph."""
    if text is None:
        return "<w:p/>"
    run_props = "<w:rPr>"
    if bold:
        run_props += "<w:b/>"
    if size_half_points:
        run_props += f'<w:sz w:val="{size_half_points}"/><w:szCs w:val="{size_half_points}"/>'
    run_props += "</w:rPr>"
    return (
        "<w:p><w:r>"
        + run_props
        + f'<w:t xml:space="preserve">{escape(text)}</w:t>'
        + "</w:r></w:p>"
    )


def _docx_bytes(title, subtitle, content):
    """Serialise a minimal, dependency-free .docx package (Word/WPS readable)."""
    paragraphs = [_docx_paragraph(title, bold=True, size_half_points=36)]
    if subtitle:
        paragraphs.append(_docx_paragraph(subtitle, bold=True, size_half_points=28))
    paragraphs.append(_docx_paragraph())
    paragraphs.extend(_docx_paragraph(line) if line else _docx_paragraph()
                      for line in content.split("\n"))
    document = (
        DOCX_DOCUMENT_OPEN + "".join(paragraphs) + "</w:body></w:document>"
    ).encode("utf-8")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as package:
        package.writestr("[Content_Types].xml", DOCX_CONTENT_TYPES)
        package.writestr("_rels/.rels", DOCX_RELS)
        package.writestr("word/document.xml", document)
    return buffer.getvalue()


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
    if fmt not in EXPORT_FORMATS: raise ValueError('unsupported export format')
    directory=Path(directory); directory.mkdir(parents=True,exist_ok=True)
    meta={'novel':novel.get('title',''),'volume':novel.get('volume',''),'chapterNumber':chapter.get('number'),'title':chapter.get('title',''),'content':chapter.get('content',''),'summary':chapter.get('summary',''),'characters':chapter.get('characters',[]),'events':chapter.get('events',[]),'foreshadowingAdded':chapter.get('foreshadowing_added',[]),'foreshadowingResolved':chapter.get('foreshadowing_resolved',[]),'review':chapter.get('review',{}),'model':chapter.get('model',''),'generatedAt':chapter.get('generated_at',''),'storyBibleVersion':novel.get('story_bible_version',0)}
    content = str(chapter.get('content', ''))
    if fmt == 'json':
        body = json.dumps(meta, ensure_ascii=False, indent=2)
    elif fmt == 'md':
        body = f"# {novel['title']}\n\n## {chapter['number']}. {chapter['title']}\n\n{content}\n"
    elif fmt == 'txt':
        body = f"{novel['title']}\n{novel.get('volume','')} 第{chapter['number']}章 {chapter['title']}\n\n{content}\n"
    path=directory/export_filename(chapter,novel,fmt)
    temporary=directory/f".{path.name}.{uuid.uuid4().hex}.tmp"
    try:
        if fmt == 'docx':
            title = f"{novel['title']}"
            subtitle = f"{novel.get('volume','')} 第{chapter['number']}章 {chapter['title']}"
            temporary.write_bytes(_docx_bytes(title, subtitle, content))
        else:
            temporary.write_text(body, encoding='utf-8')
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return path


def export_book(novel, chapters, fmt, directory):
    """Write one complete-book text file for the given chapters (in order).

    Only the two plain-text formats (txt/md) are supported for a whole
    book; a zip-based format would need its own package container.
    """
    if fmt not in ("txt", "md"):
        raise ValueError('unsupported book export format')
    title = str(novel.get('title') or '未命名小说')
    volume = str(novel.get('volume') or '').strip()
    blocks = []
    if fmt == 'md':
        blocks.append("# " + title)
        if volume:
            blocks.append("> " + volume)
        for ch in chapters:
            number = int(ch.get('number') or 0)
            ctitle = str(ch.get('title') or '').strip()
            blocks.append("")
            blocks.append("## %d. %s" % (number, ctitle))
            body = str(ch.get('content') or '').strip('\n')
            blocks.append(body)
    else:
        blocks.append(title)
        if volume:
            blocks.append(volume)
        for ch in chapters:
            number = int(ch.get('number') or 0)
            ctitle = str(ch.get('title') or '').strip()
            blocks.append("")
            blocks.append("第%d章 %s" % (number, ctitle))
            body = str(ch.get('content') or '').strip('\n')
            blocks.append(body)
    body_text = "\n".join(blocks).strip("\n") + "\n"
    base = safe_filename_component(title, '未命名小说')
    filename = "%s_全本.%s" % (base, fmt)
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / filename
    temporary = directory / (".%s.%s.tmp" % (path.name, uuid.uuid4().hex))
    try:
        temporary.write_text(body_text, encoding='utf-8')
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return path
