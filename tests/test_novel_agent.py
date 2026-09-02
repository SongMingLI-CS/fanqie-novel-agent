import json, os, tempfile, unittest
from pathlib import Path
from novel_agent.store import Store
from novel_agent.exporters import export_chapter
from novel_agent.reviewer import review
from novel_agent.service import NovelService
from novel_agent.config import Config
from novel_agent.deepseek import DeepSeekClient

class FakeClient:
    def __init__(self,text): self.text=text
    def complete(self,*args): return self.text, {'model':'test-r1','prompt_version':'novel-writer@1','input_tokens':1,'output_tokens':2,'duration_ms':1,'request_status':'succeeded'}

class NovelTests(unittest.TestCase):
    def setUp(self): self.tmp=tempfile.TemporaryDirectory(); self.store=Store(Path(self.tmp.name)/'db.sqlite3'); self.novel=self.store.create_novel('测试小说',{'characters':[{'name':'林默'}],'worldRules':[{'key':'magic','description':'规则'}],'timeline':[{'key':'e1','description':'事件'}],'foreshadowing':[{'key':'f1','status':'OPEN'}],'forbiddenContent':['SECRET']})
    def tearDown(self): self.store.close(); self.tmp.cleanup()
    def test_story_bible_and_version(self):
        self.assertEqual(self.novel['story_bible']['characters'][0]['name'],'林默'); self.assertEqual(self.store.update_bible(self.novel['id'],{'x':1}),2); self.assertEqual(self.store.get_novel(self.novel['id'])['story_bible'],{'x':1})
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM characters WHERE novel_id=?',(self.novel['id'],)).fetchone()[0],1)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM foreshadowing WHERE novel_id=?',(self.novel['id'],)).fetchone()[0],1)
    def test_generation_history_is_append_only(self):
        raw=json.dumps({'title':'一','chapterGoal':'g','content':'一。\n二。','summary':'s','beats':[]})
        job,_=self.store.create_job(self.novel['id'],1); service=NovelService(self.store,FakeClient(raw),Config(data_dir=Path(self.tmp.name)),Path(__file__).parents[1]); service.process(job); chapter=self.store.chapter(self.novel['id'],1); self.store.update_draft(chapter['id'],{'content':'编辑后'}); self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM chapter_drafts WHERE chapter_id=?',(chapter['id'],)).fetchone()[0],2)
    def test_prompt_bible_is_bounded(self):
        compact=NovelService.compact_bible({'mainline':'x'*100000,'characters':[{'name':str(i)} for i in range(1000)]})
        self.assertLessEqual(len(json.dumps(compact,ensure_ascii=False)),14000); self.assertTrue(compact.get('contextTruncated') or len(compact.get('characters',[]))<=20)
    def test_duplicate_chapter_job(self):
        a,created=self.store.create_job(self.novel['id'],1); b,created2=self.store.create_job(self.novel['id'],1); self.assertTrue(created); self.assertFalse(created2); self.assertEqual(a['id'],b['id'])
    def test_failed_job_can_resume(self):
        a,_=self.store.create_job(self.novel['id'],1); self.store.db.execute("UPDATE jobs SET status='FAILED' WHERE id=?",(a['id'],)); self.store.db.commit(); b,created=self.store.create_job(self.novel['id'],1); self.assertTrue(created); self.assertEqual(a['id'],b['id']); self.assertEqual(b['status'],'PENDING')
    def test_job_failure_retries_then_dead_letters(self):
        job,_=self.store.create_job(self.novel['id'],1); self.store.db.execute("UPDATE jobs SET attempts=1,status='RUNNING' WHERE id=?",(job['id'],)); self.store.db.commit(); self.assertTrue(self.store.fail_job(job,'temporary',3)); self.assertEqual(self.store.get_job(job['id'])['status'],'PENDING'); job=self.store.get_job(job['id']); self.store.db.execute("UPDATE jobs SET attempts=3,status='RUNNING' WHERE id=?",(job['id'],)); self.store.db.commit(); self.assertFalse(self.store.fail_job(job,'permanent',3)); self.assertEqual(self.store.get_job(job['id'])['status'],'FAILED')
    def test_paused_novel_rejects_new_job(self):
        self.store.set_paused(self.novel['id'],True)
        with self.assertRaises(ValueError): self.store.create_job(self.novel['id'],1)
    def test_review_blocks_export(self):
        out={'title':'','content':'','chapterGoal':'x'}; result=review(out,self.novel['story_bible'],[]); self.assertFalse(result['passed']); self.assertTrue(result['blockingIssues'])
        with self.assertRaises(ValueError): export_chapter({'number':1,'title':'','content':'','review':result},self.novel,'txt',Path(self.tmp.name))
    def test_exports_do_not_mix_review_into_body(self):
        ch={'number':1,'title':'开端','content':'正文','summary':'目标','characters':[],'events':[],'foreshadowing_added':[],'foreshadowing_resolved':[],'review':{'passed':True},'model':'test','generated_at':'now'}
        for fmt in ('txt','md','json'):
            p=export_chapter(ch,self.novel,fmt,Path(self.tmp.name)); self.assertTrue(p.exists()); self.assertIn('正文',p.read_text())
    def test_manual_publish(self):
        self.store.create_job(self.novel['id'],1)
        with self.assertRaises(ValueError): self.store.manual_publish(self.novel['id'],1,{'platform':'Fanqie','operator':'u'})
        self.store.db.execute("UPDATE chapters SET status='EXPORTED' WHERE novel_id=?",(self.novel['id'],)); self.store.db.commit(); self.store.manual_publish(self.novel['id'],1,{'platform':'Fanqie','operator':'u'}); self.assertEqual(self.store.chapter(self.novel['id'],1)['status'],'PUBLISHED_MANUALLY'); self.assertEqual(self.store.get_novel(self.novel['id'])['current_chapter'],1)
    def test_manual_publish_applies_proposed_story_state(self):
        self.store.create_job(self.novel['id'],1); ch=self.store.chapter(self.novel['id'],1); self.store.db.execute("UPDATE chapters SET status='EXPORTED',proposed_state=? WHERE id=?",(json.dumps({'events':[{'key':'new-event'}],'foreshadowingResolved':['f1']}),ch['id'])); self.store.db.commit(); self.store.manual_publish(self.novel['id'],1,{'platform':'manual','operator':'u'}); novel=self.store.get_novel(self.novel['id']); self.assertEqual(novel['story_bible_version'],2); self.assertEqual(novel['story_bible']['currentChapter'],1); self.assertEqual(novel['story_bible']['foreshadowing'][0]['status'],'RESOLVED')
    def test_review_without_pass_cannot_export(self):
        result=review({'title':'x','chapterGoal':'g','content':'正文'},self.novel['story_bible'],[],target_words=1000); self.assertFalse(result['passed']); self.assertFalse(result['blockingIssues'])
    def test_review_blocks_world_timeline_and_foreshadow_conflicts(self):
        bible={'characters':[],'worldRules':[{'key':'magic'}],'timeline':[{'key':'known'}],'foreshadowing':[{'key':'open','status':'OPEN'}]}
        result=review({'title':'x','chapterGoal':'g','content':'一。\n二。','stateChanges':[{'rule':'unknown'}],'eventsIntroduced':[{'key':'known'}],'foreshadowingResolved':['missing']},bible,[])
        self.assertFalse(result['passed']); self.assertIn('unauthorized_world_rule:unknown',result['blockingIssues']); self.assertIn('timeline_event_redefinition:known',result['blockingIssues']); self.assertIn('foreshadowing_not_open:missing',result['blockingIssues'])
    def test_review_rejects_non_object(self):
        self.assertIn('invalid_structured_output',review([],{},[])['blockingIssues'])
    def test_expired_running_job_is_recovered(self):
        job,_=self.store.create_job(self.novel['id'],1); self.store.db.execute("UPDATE jobs SET status='RUNNING',locked_until='2000-01-01T00:00:00+00:00' WHERE id=?",(job['id'],)); self.store.db.commit(); claimed=self.store.claim_job(); self.assertEqual(claimed['id'],job['id']); self.assertEqual(claimed['status'],'RUNNING'); self.assertEqual(self.store.get_job(job['id'])['attempts'],1)
    def test_edit_invalidates_review(self):
        self.store.create_job(self.novel['id'],1); self.store.db.execute("UPDATE chapters SET title='旧',content='正文',review=? WHERE novel_id=?",(json.dumps({'passed':True}),self.novel['id'])); self.store.db.commit(); ch=self.store.chapter(self.novel['id'],1); edited=self.store.update_draft(ch['id'],{'content':'修改后'}); self.assertEqual(edited['status'],'REVIEWING'); self.assertEqual(edited['review'],{})
    def test_generation_structured_response(self):
        raw=json.dumps({'chapterNumber':1,'title':'开端','chapterGoal':'找到线索','summary':'林默发现线索','beats':[{'goal':'调查'}],'content':'第一段。\n\n第二段。','charactersUsed':[],'eventsIntroduced':[],'foreshadowingAdded':[],'foreshadowingResolved':[],'stateChanges':[],'nextChapterHook':'门开了','warnings':[]})
        job,_=self.store.create_job(self.novel['id'],1); service=NovelService(self.store,FakeClient(raw),Config(data_dir=Path(self.tmp.name)),Path(__file__).parents[1]); self.assertTrue(service.process(job)); chapter=self.store.chapter(self.novel['id'],1); self.assertEqual(chapter['status'],'WAITING_APPROVAL'); self.assertEqual(chapter['summary'],'林默发现线索'); self.assertEqual(chapter['beats'][0]['goal'],'调查'); self.assertEqual(self.store.get_novel(self.novel['id'])['current_chapter'],0)
    def test_invalid_model_response_fails(self):
        job,_=self.store.create_job(self.novel['id'],1); service=NovelService(self.store,FakeClient('not json'),Config(data_dir=Path(self.tmp.name)),Path(__file__).parents[1]); self.assertFalse(service.process(job)); self.assertEqual(self.store.chapter(self.novel['id'],1)['status'],'FAILED')
    def test_deepseek_timeout_retries(self):
        class Response:
            def __enter__(self): return self
            def __exit__(self,*args): pass
            def read(self): return b'{"choices":[{"message":{"content":"{}"}}],"usage":{"prompt_tokens":3,"completion_tokens":4}}'
        calls=[]
        def opener(*args,**kwargs):
            calls.append(1)
            if len(calls)<3: raise TimeoutError('timeout')
            return Response()
        old=os.environ.get('DEEPSEEK_API_KEY'); os.environ['DEEPSEEK_API_KEY']='test-only'
        try:
            c=DeepSeekClient(Config(max_retries=2,timeout=1),opener); text,usage=c.complete('s','u'); self.assertEqual(text,'{}'); self.assertEqual(len(calls),3); self.assertEqual(usage['output_tokens'],4)
        finally:
            if old is None: os.environ.pop('DEEPSEEK_API_KEY',None)
            else: os.environ['DEEPSEEK_API_KEY']=old
    def test_api_key_not_in_frontend(self):
        self.assertNotIn('DEEPSEEK_API_KEY',Path(__file__).parents[1].joinpath('static/index.html').read_text())
        self.assertNotIn('${c.title}',Path(__file__).parents[1].joinpath('static/index.html').read_text())

if __name__=='__main__': unittest.main()
