import json
from pathlib import Path
from .deepseek import DeepSeekError, parse_output
from .reviewer import review


class NovelService:
    def __init__(self, store, client, config, root=None): self.store=store; self.client=client; self.config=config; self.root=Path(root or Path(__file__).parents[1])
    def context(self,nid,number):
        novel=self.store.get_novel(nid); recent=self.store.recent(nid); bible=novel['story_bible']
        refs=self.root/'.agents/skills/novel-writer/references'; skill='\n'.join((refs/x).read_text(encoding='utf-8') for x in ('story-bible.md','style-rules.md','chapter-template.md','review-rubric.md'))
        return novel,recent,bible,skill
    def process(self,job):
        novel,recent,bible,skill=self.context(job['novel_id'],job['chapter_number']); self.store.set_status(job['novel_id'],job['chapter_number'],'PLANNING')
        if novel['paused']:
            self.store.set_status(job['novel_id'],job['chapter_number'],'CANCELLED')
            self.store.db.execute("UPDATE jobs SET status='CANCELLED',error=? WHERE id=?",('novel_paused',job['id'])); self.store.db.commit(); return False
        system='You are a structured novel writer. Follow the supplied project skill and Story Bible. Return only valid JSON.'
        prompt=json.dumps({'skill':skill,'storyBible':bible,'recentChapterSummaries':[x.get('summary','') for x in recent],'chapterNumber':job['chapter_number'],'request':'Plan beats and write the next chapter. Respect all facts; do not invent unauthorized key settings.'},ensure_ascii=False)
        self.store.set_status(job['novel_id'],job['chapter_number'],'GENERATING')
        json_failure=False
        try:
            raw,usage=self.client.complete(system,prompt)
            try: output=parse_output(raw)
            except DeepSeekError:
                # One bounded repair/retry, never publish partial text.
                json_failure=True
                raw,usage=self.client.complete(system,prompt+'\nThe previous response was invalid JSON. Return the same chapter as one valid JSON object.')
                output=parse_output(raw)
        except DeepSeekError as exc:
            self.store.fail_job(job,str(exc),0 if json_failure else self.config.max_job_attempts); return False
        self.store.set_status(job['novel_id'],job['chapter_number'],'REVIEWING'); result=review(output,bible,recent)
        proposed={'currentChapter':job['chapter_number'],'stateChanges':output.get('stateChanges',[]),'events':output.get('eventsIntroduced',[]),'foreshadowingResolved':output.get('foreshadowingResolved',[])}
        self.store.save_generation(job,output,raw,result,usage,proposed); return result['passed']
