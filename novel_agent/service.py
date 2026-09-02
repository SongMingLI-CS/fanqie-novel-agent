import json
from pathlib import Path
from .deepseek import DeepSeekError, parse_output, response_summary, validate_chapter_output
from .reviewer import review


class NovelService:
    def __init__(self, store, client, config, root=None): self.store=store; self.client=client; self.config=config; self.root=Path(root or Path(__file__).parents[1])
    def context(self,nid,number):
        novel=self.store.get_novel(nid); recent=self.store.recent(nid); bible=self.compact_bible(novel['story_bible'])
        refs=self.root/'.agents/skills/novel-writer/references'; skill='\n'.join((refs/x).read_text(encoding='utf-8') for x in ('story-bible.md','style-rules.md','chapter-template.md','review-rubric.md'))
        return novel,recent,bible,skill

    @staticmethod
    def compact_bible(bible, limit=14000):
        """Keep prompt context bounded while retaining facts needed for the next chapter."""
        if not isinstance(bible,dict): return {}
        selected={}
        for key in ('title','genre','mainline','finalGoal','currentPosition','nextStageGoal','forbiddenContent','worldRules','characters','timeline','foreshadowing'):
            value=bible.get(key)
            if isinstance(value,list): value=value[:20]
            elif isinstance(value,str): value=value[:2000]
            selected[key]=value
        encoded=json.dumps(selected,ensure_ascii=False)
        return json.loads(encoded) if len(encoded)<=limit else {'contextTruncated':True,'facts':encoded[:max(0,limit-100)]}
    def process(self,job):
        novel,recent,bible,skill=self.context(job['novel_id'],job['chapter_number']); self.store.set_status(job['novel_id'],job['chapter_number'],'PLANNING')
        if novel['paused']:
            self.store.set_status(job['novel_id'],job['chapter_number'],'CANCELLED')
            self.store.db.execute("UPDATE jobs SET status='CANCELLED',error=? WHERE id=?",('novel_paused',job['id'])); self.store.db.commit(); return False
        system='You are a structured novel writer. Follow the supplied project skill and Story Bible. Return only valid JSON.'
        prompt=json.dumps({'skill':skill,'storyBible':bible,'recentChapterSummaries':[x.get('summary','') for x in recent],'chapterNumber':job['chapter_number'],'request':'Plan beats and write the next chapter. Include a concise summary field. Respect all facts; do not invent unauthorized key settings.'},ensure_ascii=False)
        self.store.set_status(job['novel_id'],job['chapter_number'],'GENERATING')
        json_failure=False; first_failure=''
        try:
            raw,usage=self.client.complete(system,prompt)
            try:
                output=validate_chapter_output(parse_output(raw),job['chapter_number'])
            except DeepSeekError:
                # One bounded repair/retry, never publish partial text.
                json_failure=True; first_failure=response_summary(raw)
                repair_prompt=prompt+'\n上一次响应无法解析或不符合字段契约。请修复后只返回一个完整、合法的 JSON 对象，不要 Markdown 代码块、解释文字或前后缀；正文必须完整放在 content 字符串中，正确转义引号、换行和 Unicode。'
                raw,usage=self.client.complete(system,repair_prompt)
                output=validate_chapter_output(parse_output(raw),job['chapter_number'])
        except DeepSeekError as exc:
            detail=str(exc)
            if json_failure: detail += '; first_response_summary='+first_failure
            self.store.record_usage(job,self.config.model,'novel-writer@1','failed',detail)
            self.store.fail_job(job,detail,0 if json_failure else self.config.max_job_attempts); return False
        self.store.set_status(job['novel_id'],job['chapter_number'],'REVIEWING'); rules=bible.get('styleRules',{}) if isinstance(bible,dict) else {}; target=rules.get('chapterLength',0) if isinstance(rules,dict) else 0; result=review(output,bible,recent,target if isinstance(target,int) else 0)
        proposed={'currentChapter':job['chapter_number'],'stateChanges':output.get('stateChanges',[]),'events':output.get('eventsIntroduced',[]),'foreshadowingResolved':output.get('foreshadowingResolved',[])}
        self.store.save_generation(job,output,raw,result,usage,proposed); return result['passed']
