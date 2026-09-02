import json, os, tempfile, unittest
from pathlib import Path
from novel_agent.store import Store
from novel_agent.exporters import export_chapter
from novel_agent.reviewer import review
from novel_agent.service import NovelService
from novel_agent.config import Config

class FakeClient:
    def __init__(self,text): self.text=text
    def complete(self,*args): return self.text, {'model':'test-r1','prompt_version':'novel-writer@1','input_tokens':1,'output_tokens':2,'duration_ms':1,'request_status':'succeeded'}

class NovelTests(unittest.TestCase):
    def setUp(self): self.tmp=tempfile.TemporaryDirectory(); self.store=Store(Path(self.tmp.name)/'db.sqlite3'); self.novel=self.store.create_novel('测试小说',{'characters':[{'name':'林默'}],'forbiddenContent':['SECRET']})
    def tearDown(self): self.tmp.cleanup()
    def test_story_bible_and_version(self):
        self.assertEqual(self.novel['story_bible']['characters'][0]['name'],'林默'); self.assertEqual(self.store.update_bible(self.novel['id'],{'x':1}),2); self.assertEqual(self.store.get_novel(self.novel['id'])['story_bible'],{'x':1})
    def test_duplicate_chapter_job(self):
        a,created=self.store.create_job(self.novel['id'],1); b,created2=self.store.create_job(self.novel['id'],1); self.assertTrue(created); self.assertFalse(created2); self.assertEqual(a['id'],b['id'])
    def test_failed_job_can_resume(self):
        a,_=self.store.create_job(self.novel['id'],1); self.store.db.execute("UPDATE jobs SET status='FAILED' WHERE id=?",(a['id'],)); self.store.db.commit(); b,created=self.store.create_job(self.novel['id'],1); self.assertTrue(created); self.assertEqual(a['id'],b['id']); self.assertEqual(b['status'],'PENDING')
    def test_review_blocks_export(self):
        out={'title':'','content':'','chapterGoal':'x'}; result=review(out,self.novel['story_bible'],[]); self.assertFalse(result['passed']); self.assertTrue(result['blockingIssues'])
        with self.assertRaises(ValueError): export_chapter({'number':1,'title':'','content':'','review':result},self.novel,'txt',Path(self.tmp.name))
    def test_exports_do_not_mix_review_into_body(self):
        ch={'number':1,'title':'开端','content':'正文','summary':'目标','characters':[],'events':[],'foreshadowing_added':[],'foreshadowing_resolved':[],'review':{'passed':True},'model':'test','generated_at':'now'}
        for fmt in ('txt','md','json'):
            p=export_chapter(ch,self.novel,fmt,Path(self.tmp.name)); self.assertTrue(p.exists()); self.assertIn('正文',p.read_text())
    def test_manual_publish(self):
        self.store.create_job(self.novel['id'],1); self.store.db.execute("UPDATE chapters SET status='EXPORTED' WHERE novel_id=?",(self.novel['id'],)); self.store.db.commit(); self.store.manual_publish(self.novel['id'],1,{'platform':'Fanqie','operator':'u'}); self.assertEqual(self.store.chapter(self.novel['id'],1)['status'],'PUBLISHED_MANUALLY')
    def test_generation_structured_response(self):
        raw=json.dumps({'chapterNumber':1,'title':'开端','chapterGoal':'找到线索','content':'第一段。\n\n第二段。','charactersUsed':[],'eventsIntroduced':[],'foreshadowingAdded':[],'foreshadowingResolved':[],'stateChanges':[],'nextChapterHook':'门开了','warnings':[]})
        job,_=self.store.create_job(self.novel['id'],1); service=NovelService(self.store,FakeClient(raw),Config(data_dir=Path(self.tmp.name)),Path(__file__).parents[1]); self.assertTrue(service.process(job)); self.assertEqual(self.store.chapter(self.novel['id'],1)['status'],'WAITING_APPROVAL'); self.assertEqual(self.store.get_novel(self.novel['id'])['current_chapter'],0)
    def test_invalid_model_response_fails(self):
        job,_=self.store.create_job(self.novel['id'],1); service=NovelService(self.store,FakeClient('not json'),Config(data_dir=Path(self.tmp.name)),Path(__file__).parents[1]); self.assertFalse(service.process(job)); self.assertEqual(self.store.chapter(self.novel['id'],1)['status'],'FAILED')

if __name__=='__main__': unittest.main()
