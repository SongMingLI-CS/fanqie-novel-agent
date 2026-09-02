"""Safe publishing boundaries. No browser automation or platform login lives here."""
from pathlib import Path
from typing import Protocol

from .exporters import export_chapter


class ExternalPublisher(Protocol):
    def validate(self, chapter: dict) -> None: ...
    def publish(self, chapter: dict) -> dict: ...
    def confirm(self, external_id: str) -> dict: ...


class DryRunPublisher:
    def validate(self, chapter):
        if not chapter.get('content','').strip(): raise ValueError('empty_chapter')

    def publish(self, chapter):
        self.validate(chapter)
        return {'status':'DRY_RUN','chapterId':chapter.get('id'),'sideEffect':False}

    def confirm(self, external_id): return {'status':'DRY_RUN_CONFIRMED','externalId':external_id}


class LocalFilePublisher:
    def __init__(self, directory): self.directory=Path(directory)

    def validate(self, chapter):
        if not chapter.get('content','').strip(): raise ValueError('empty_chapter')
        if not chapter.get('review',{}).get('passed') or chapter.get('review',{}).get('blockingIssues'): raise ValueError('chapter_review_prevents_export')

    def publish(self, chapter, novel, fmt='txt'):
        self.validate(chapter); path=export_chapter(chapter,novel,fmt,self.directory)
        return {'status':'EXPORTED','path':str(path),'sideEffect':'local_file_only'}

    def confirm(self, external_id): raise ValueError('local_file_publisher_has_no_external_confirmation')
