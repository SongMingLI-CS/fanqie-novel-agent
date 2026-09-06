import json, os, tempfile, unittest
from pathlib import Path
from novel_agent.store import Store
from novel_agent.exporters import export_chapter, export_filename
from novel_agent.reviewer import review
from novel_agent.service import NovelService
from novel_agent.config import Config
from novel_agent.deepseek import DeepSeekClient, parse_output, validate_chapter_output
from novel_agent.worker import run_once

class FakeClient:
    def __init__(self,text): self.text=text
    def complete(self,*args): return self.text, {'model':'test-r1','prompt_version':'novel-writer@1','input_tokens':1,'output_tokens':2,'duration_ms':1,'request_status':'succeeded'}

def chapter_json(content='第一段。\n\n他说：“继续。”'):
    return json.dumps({'chapterNumber':1,'title':'开端','chapterGoal':'找到线索','summary':'发现线索','beats':[{'goal':'调查'}],'content':content,'charactersUsed':[],'eventsIntroduced':[],'foreshadowingAdded':[],'foreshadowingResolved':[],'stateChanges':[],'nextChapterHook':'门开了','warnings':[]},ensure_ascii=False)

class SequenceClient(FakeClient):
    def __init__(self, texts): self.texts=list(texts); self.calls=[]
    def complete(self, system, user):
        self.calls.append(user); text=self.texts.pop(0); return text, {'model':'test-r1','prompt_version':'novel-writer@1','input_tokens':1,'output_tokens':2,'duration_ms':1,'request_status':'succeeded'}

class NovelTests(unittest.TestCase):
    def setUp(self): self.tmp=tempfile.TemporaryDirectory(); self.store=Store(Path(self.tmp.name)/'db.sqlite3'); self.novel=self.store.create_novel('测试小说',{'characters':[{'name':'林默'}],'worldRules':[{'key':'magic','description':'规则'}],'timeline':[{'key':'e1','description':'事件'}],'foreshadowing':[{'key':'f1','status':'OPEN'}],'forbiddenContent':['SECRET']})
    def tearDown(self): self.store.close(); self.tmp.cleanup()
    def test_story_bible_and_version(self):
        self.assertEqual(self.novel['story_bible']['characters'][0]['name'],'林默'); self.assertEqual(self.store.update_bible(self.novel['id'],{'x':1}),2); self.assertEqual(self.store.get_novel(self.novel['id'])['story_bible'],{'x':1})
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM characters WHERE novel_id=?',(self.novel['id'],)).fetchone()[0],1)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM foreshadowing WHERE novel_id=?',(self.novel['id'],)).fetchone()[0],1)
    def test_generation_history_is_append_only(self):
        raw=chapter_json('一。\n\n二。')
        job,_=self.store.create_job(self.novel['id'],1); service=NovelService(self.store,FakeClient(raw),Config(data_dir=Path(self.tmp.name)),Path(__file__).parents[1]); service.process(job); chapter=self.store.chapter(self.novel['id'],1); self.store.update_draft(chapter['id'],{'content':'编辑后'}); self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM chapter_drafts WHERE chapter_id=?',(chapter['id'],)).fetchone()[0],2)
    def test_prompt_bible_is_bounded(self):
        compact=NovelService.compact_bible({'mainline':'x'*100000,'characters':[{'name':str(i)} for i in range(1000)]})
        self.assertLessEqual(len(json.dumps(compact,ensure_ascii=False)),14000); self.assertTrue(compact.get('contextTruncated') or len(compact.get('characters',[]))<=20)
    def test_duplicate_chapter_job(self):
        a,created=self.store.create_job(self.novel['id'],1); b,created2=self.store.create_job(self.novel['id'],1); self.assertTrue(created); self.assertFalse(created2); self.assertEqual(a['id'],b['id'])
    def test_failed_job_can_resume(self):
        a,_=self.store.create_job(self.novel['id'],1); self.store.db.execute("UPDATE jobs SET status='FAILED' WHERE id=?",(a['id'],)); self.store.db.commit(); b,created=self.store.create_job(self.novel['id'],1); self.assertTrue(created); self.assertEqual(a['id'],b['id']); self.assertEqual(b['status'],'PENDING')
    def test_job_failure_retries_then_dead_letters(self):
        job,_=self.store.create_job(self.novel['id'],1); self.store.db.execute("UPDATE jobs SET attempts=1,status='RUNNING' WHERE id=?",(job['id'],)); self.store.db.commit(); self.assertTrue(self.store.fail_job(job,'temporary',3)); delayed=self.store.get_job(job['id']); self.assertEqual(delayed['status'],'PENDING'); self.assertTrue(delayed['next_attempt_at']); self.assertIsNone(self.store.claim_job()); self.store.db.execute("UPDATE jobs SET next_attempt_at='2000-01-01T00:00:00+00:00' WHERE id=?",(job['id'],)); self.store.db.commit(); self.assertIsNotNone(self.store.claim_job()); job=self.store.get_job(job['id']); self.store.db.execute("UPDATE jobs SET attempts=3,status='RUNNING' WHERE id=?",(job['id'],)); self.store.db.commit(); self.assertFalse(self.store.fail_job(job,'permanent',3)); self.assertEqual(self.store.get_job(job['id'])['status'],'FAILED')
    def test_paused_novel_rejects_new_job(self):
        self.store.set_paused(self.novel['id'],True)
        with self.assertRaises(ValueError): self.store.create_job(self.novel['id'],1)
    def test_cancel_job_cancels_chapter_atomically(self):
        job,_=self.store.create_job(self.novel['id'],1); self.assertTrue(self.store.cancel_job(job['id'])); self.assertEqual(self.store.get_job(job['id'])['status'],'CANCELLED'); self.assertEqual(self.store.chapter(self.novel['id'],1)['status'],'CANCELLED'); self.assertFalse(self.store.cancel_job(job['id']))
    def test_rewrite_latest_draft_clears_history_and_requeues(self):
        self.store.create_job(self.novel['id'],1); ch=self.store.chapter(self.novel['id'],1); cid=ch['id']; conn=self.store.db
        conn.execute("INSERT INTO chapter_drafts VALUES (?,?,?,?,?,?,?)",('draft1',cid,1,json.dumps({'title':'旧'}),'','{}','2026-01-01T00:00:00+00:00'))
        conn.execute("UPDATE chapters SET status='WAITING_APPROVAL',title='旧标题',content='旧正文',review=? WHERE id=?",(json.dumps({'passed':True}),cid))
        conn.execute("UPDATE jobs SET status='SUCCEEDED' WHERE novel_id=? AND chapter_number=1",(self.novel['id'],)); conn.commit()
        job=self.store.rewrite_chapter(cid)
        self.assertEqual(job['status'],'PENDING'); self.assertEqual(job['chapter_number'],1); self.assertEqual(self.store.get_job(job['id'])['idempotency_key'],f"{self.novel['id']}:1:generate")
        fresh=self.store.chapter(self.novel['id'],1); self.assertEqual(fresh['id'],cid); self.assertEqual(fresh['status'],'PENDING'); self.assertEqual(fresh['title'],''); self.assertEqual(fresh['content'],''); self.assertEqual(fresh['review'],{})
        self.assertEqual(conn.execute('SELECT COUNT(*) FROM chapter_drafts WHERE chapter_id=?',(cid,)).fetchone()[0],0)
        self.assertEqual(conn.execute('SELECT COUNT(*) FROM jobs WHERE novel_id=?',(self.novel['id'],)).fetchone()[0],1)
    def test_rewrite_requires_latest_unpublished_chapter(self):
        self.store.create_job(self.novel['id'],1); self.store.create_job(self.novel['id'],2)
        self.store.db.execute("UPDATE chapters SET status='WAITING_APPROVAL',content='x' WHERE novel_id=? AND number=1",(self.novel['id'],)); self.store.db.commit()
        first=self.store.chapter(self.novel['id'],1)
        with self.assertRaisesRegex(ValueError,'only_latest_draft_can_be_rewritten'): self.store.rewrite_chapter(first['id'])
    def test_rewrite_rejects_published_chapter(self):
        self.store.create_job(self.novel['id'],1); ch=self.store.chapter(self.novel['id'],1); self.store.db.execute("UPDATE chapters SET status='EXPORTED' WHERE id=?",(ch['id'],)); self.store.db.commit(); self.store.manual_publish(self.novel['id'],1,{'platform':'Fanqie','operator':'u'})
        ch=self.store.chapter(self.novel['id'],1); self.assertEqual(ch['status'],'PUBLISHED_MANUALLY')
        with self.assertRaisesRegex(ValueError,'chapter_already_published'): self.store.rewrite_chapter(ch['id'])
    def test_rewrite_rejects_active_generation(self):
        self.store.create_job(self.novel['id'],1); ch=self.store.chapter(self.novel['id'],1)
        with self.assertRaisesRegex(ValueError,'chapter_busy'): self.store.rewrite_chapter(ch['id'])
    def test_review_blocks_export(self):
        out={'title':'','content':'','chapterGoal':'x'}; result=review(out,self.novel['story_bible'],[]); self.assertFalse(result['passed']); self.assertTrue(result['blockingIssues'])
        with self.assertRaises(ValueError): export_chapter({'number':1,'title':'','content':'','review':result},self.novel,'txt',Path(self.tmp.name))
    def test_exports_do_not_mix_review_into_body(self):
        ch={'number':1,'title':'开端','content':'正文','summary':'目标','characters':[],'events':[],'foreshadowing_added':[],'foreshadowing_resolved':[],'review':{'passed':True},'model':'test','generated_at':'now'}
        for fmt in ('txt','md','json'):
            p=export_chapter(ch,self.novel,fmt,Path(self.tmp.name)); self.assertTrue(p.exists()); self.assertIn('正文',p.read_text(encoding="utf-8")); self.assertFalse(list(Path(self.tmp.name).glob('*.tmp')))
    def test_export_docx_is_valid_ooxml_with_escaped_text(self):
        import zipfile
        ch={'number':1,'title':'开端','content':'第一段 <符号&名字>。\n\n第二段。','summary':'x','characters':[],'events':[],'foreshadowing_added':[],'foreshadowing_resolved':[],'review':{'passed':True},'model':'test','generated_at':'now'}
        p=export_chapter(ch,self.novel,'docx',Path(self.tmp.name)); self.assertTrue(p.exists()); self.assertEqual(p.suffix,'.docx')
        self.assertFalse(list(Path(self.tmp.name).glob('*.tmp')))
        with zipfile.ZipFile(p) as archive:
            names=set(archive.namelist())
            self.assertIn('[Content_Types].xml',names); self.assertIn('_rels/.rels',names); self.assertIn('word/document.xml',names)
            xml=archive.read('word/document.xml').decode('utf-8')
        self.assertIn('第一段 &lt;符号&amp;名字&gt;。',xml); self.assertIn('开端',xml); self.assertIn('<w:document',xml); self.assertNotIn('blockingIssues',xml)
    def test_export_filename_is_readable_and_path_safe(self):
        name=export_filename({'number':7,'title':'开端/转折:*?'},{'title':'测试<小说>','volume':'卷一：启程'},'txt')
        self.assertEqual(name,'测试_小说_卷一_启程_第0007章_开端_转折.txt')
        self.assertNotIn('/',name); self.assertNotIn('..',name)
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
        exported=Path(self.tmp.name)/'exports'/'测试小说_第0001章_开端.txt'; self.assertTrue(exported.exists()); self.assertIn('第一段。',exported.read_text(encoding="utf-8")); self.assertNotIn('blockingIssues',exported.read_text(encoding="utf-8")); self.assertIsNone(self.store.export_job(chapter,'txt'))
    def test_invalid_model_response_fails(self):
        job,_=self.store.create_job(self.novel['id'],1); service=NovelService(self.store,FakeClient('not json'),Config(data_dir=Path(self.tmp.name)),Path(__file__).parents[1]); self.assertFalse(service.process(job)); self.assertEqual(self.store.chapter(self.novel['id'],1)['status'],'FAILED'); self.assertEqual(self.store.usage(self.novel['id'])[0]['request_status'],'failed'); self.assertFalse((Path(self.tmp.name)/'exports').exists())

    def test_json_parser_handles_fence_prefix_and_unicode_text(self):
        value=parse_output('说明文字\n```json\n'+chapter_json()+'\n```\n结束')
        self.assertEqual(value['content'],'第一段。\n\n他说：“继续。”')
        self.assertEqual(validate_chapter_output(value,1)['chapterNumber'],1)

    def test_reasoning_content_is_ignored(self):
        class Response:
            def __enter__(self): return self
            def __exit__(self,*args): pass
            def read(self): return json.dumps({'choices':[{'message':{'reasoning_content':'not json','content':'answer'}}]}).encode()
        old=os.environ.get('DEEPSEEK_API_KEY'); os.environ['DEEPSEEK_API_KEY']='test-only'
        try:
            text,_=DeepSeekClient(Config(max_retries=0,base_url='https://test.invalid',model='test-model'),lambda *a,**k: Response()).complete('s','u'); self.assertEqual(text,'answer')
        finally:
            if old is None: os.environ.pop('DEEPSEEK_API_KEY',None)
            else: os.environ['DEEPSEEK_API_KEY']=old

    def test_invalid_json_retries_same_job_and_succeeds(self):
        client=SequenceClient(['说明\n{截断',chapter_json()]); job,_=self.store.create_job(self.novel['id'],1)
        self.assertTrue(NovelService(self.store,client,Config(data_dir=Path(self.tmp.name)),Path(__file__).parents[1]).process(job))
        self.assertEqual(len(client.calls),2); self.assertEqual(self.store.get_job(job['id'])['status'],'SUCCEEDED'); self.assertEqual(self.store.chapter(self.novel['id'],1)['status'],'WAITING_APPROVAL')

    def test_two_invalid_json_responses_fail_without_story_bible_change(self):
        client=SequenceClient(['前缀 {截断','仍然不是 JSON']); job,_=self.store.create_job(self.novel['id'],1); before=self.store.get_novel(self.novel['id'])
        self.assertFalse(NovelService(self.store,client,Config(data_dir=Path(self.tmp.name)),Path(__file__).parents[1]).process(job))
        self.assertEqual(len(client.calls),2); self.assertEqual(self.store.get_job(job['id'])['status'],'FAILED'); self.assertEqual(self.store.get_novel(self.novel['id'])['story_bible_version'],before['story_bible_version']); self.assertEqual(self.store.chapter(self.novel['id'],1)['content'],''); self.assertIn('first_response_summary=',self.store.usage(self.novel['id'])[0]['error'])
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
            c=DeepSeekClient(Config(max_retries=2,timeout=180,base_url='https://test.invalid',model='test-model'),opener); text,usage=c.complete('s','u'); self.assertEqual(text,'{}'); self.assertEqual(len(calls),3); self.assertEqual(usage['output_tokens'],4)
        finally:
            if old is None: os.environ.pop('DEEPSEEK_API_KEY',None)
            else: os.environ['DEEPSEEK_API_KEY']=old

    def test_deepseek_uses_stream_and_separate_timeout_settings(self):
        class Response:
            status=200
            def __enter__(self): return self
            def __exit__(self,*args): pass
            def read(self): return b'data: {"choices":[{"delta":{"content":"{\\"chapterNumber\\":1"}}]}\n\ndata: {"choices":[{"delta":{"content":"}"}}],"usage":{"prompt_tokens":5,"completion_tokens":6}}\n\ndata: [DONE]\n'
        calls=[]
        def opener(request,timeout,context):
            calls.append((json.loads(request.data),timeout)); return Response()
        old=os.environ.get('DEEPSEEK_API_KEY'); os.environ['DEEPSEEK_API_KEY']='test-only'
        try:
            config=Config(max_retries=0,timeout=180,connect_timeout=10,base_url='https://test.invalid',model='test-model')
            text,usage=DeepSeekClient(config,opener).complete('s','u')
            self.assertEqual(json.loads(text)['chapterNumber'],1); self.assertEqual(calls[0][0]['stream'],True); self.assertEqual(calls[0][0]['stream_options'],{'include_usage':True}); self.assertEqual(calls[0][0]['response_format'],{'type':'json_object'}); self.assertEqual(calls[0][0]['thinking'],{'type':'disabled'}); self.assertEqual(calls[0][1],180); self.assertEqual(usage['output_tokens'],6)
        finally:
            if old is None: os.environ.pop('DEEPSEEK_API_KEY',None)
            else: os.environ['DEEPSEEK_API_KEY']=old
    def test_deepseek_requires_injected_base_url(self):
        old=os.environ.get('DEEPSEEK_API_KEY'); os.environ['DEEPSEEK_API_KEY']='test-only'
        try:
            with self.assertRaisesRegex(Exception,'BASE_URL'): DeepSeekClient(Config(base_url='')).complete('s','u')
        finally:
            if old is None: os.environ.pop('DEEPSEEK_API_KEY',None)
            else: os.environ['DEEPSEEK_API_KEY']=old
    def test_frontend_exposes_no_api_secret(self):
        html = Path(__file__).parents[1].joinpath('static/index.html').read_text(encoding="utf-8")
        # The dashboard names DEEPSEEK_API_KEY in its repair guidance and echoes
        # backend error strings verbatim, so the env-var NAME is expected in the
        # HTML. What must never ship is a real (or real-looking) secret.
        self.assertNotRegex(html, r'sk-[A-Za-z0-9]{16,}')
        self.assertNotRegex(html, r'DEEPSEEK_API_KEY\s*=\s*["\']?[A-Za-z0-9]{12,}')
        self.assertNotIn('${c.title}', html)

    def test_worker_marks_crashed_job_failed_when_attempts_exhausted(self):
        # A code-level crash must not leave the job RUNNING until lease expiry and
        # then retry forever: run_once() catches it and marks the job FAILED once
        # max_job_attempts is reached.
        job,_=self.store.create_job(self.novel['id'],1)
        class Boom(FakeClient):
            def complete(self,system,user): raise RuntimeError('boom')
        service=NovelService(self.store,Boom(''),Config(data_dir=Path(self.tmp.name),max_job_attempts=1),Path(__file__).parents[1])
        self.assertTrue(run_once(self.store,service))
        row=self.store.get_job(job['id'])
        self.assertEqual(row['status'],'FAILED')
        self.assertIn('worker_crash:RuntimeError',row['error'])
        self.assertEqual(self.store.chapter(self.novel['id'],1)['status'],'FAILED')

    def test_worker_crash_retries_with_backoff_before_failing(self):
        # With headroom left in max_job_attempts, a crash is scheduled for retry
        # (PENDING + next_attempt_at) instead of dead-lettering immediately.
        job,_=self.store.create_job(self.novel['id'],1)
        class Boom(FakeClient):
            def complete(self,system,user): raise ValueError('temporary')
        service=NovelService(self.store,Boom(''),Config(data_dir=Path(self.tmp.name),max_job_attempts=3),Path(__file__).parents[1])
        self.assertTrue(run_once(self.store,service))
        row=self.store.get_job(job['id'])
        self.assertEqual(row['status'],'PENDING')
        self.assertIn('worker_crash:ValueError',row['error'])
        self.assertIsNotNone(row['next_attempt_at'])

    # ---- Correctness regression: native StoryBible keys must survive ----

    def test_compact_bible_preserves_native_keys_and_bounded_facts(self):
        # Real novels store people/arcs under protagonist/mainCharacters/storyArcs,
        # not only under the template whitelist. compact_bible must keep them.
        native={'protagonist':{'name':'林默','motivation':'为父报仇'*1000},
                'mainCharacters':[{'name':f'配角{i}','role':'盟友'} for i in range(30)],
                'storyArcs':[{'arc':'夺宝','goal':'集齐碎片'}],
                'worldRules':[{'key':'magic','description':'灵气'}],
                'timeline':[{'key':'e1'}]}
        compact=NovelService.compact_bible(native)
        self.assertNotIn('contextTruncated',compact)
        self.assertEqual(compact['protagonist']['name'],'林默')
        self.assertEqual(compact['protagonist']['motivation'],('为父报仇'*1000)[:2000])
        self.assertEqual(len(compact['mainCharacters']),20)
        self.assertIn('storyArcs',compact); self.assertIn('worldRules',compact); self.assertIn('timeline',compact)
        self.assertEqual(compact['storyArcs'][0]['arc'],'夺宝')
        self.assertLessEqual(len(json.dumps(compact,ensure_ascii=False)),14000)

    def test_compact_bible_does_not_inject_null_template_keys(self):
        # A native bible that has no template sections must not gain phantom
        # null keys (the old whitelist added characters/worldRules/... as None).
        compact=NovelService.compact_bible({'protagonist':{'name':'林默'}})
        self.assertEqual(set(compact.keys()),{'protagonist'})

    def test_compact_bible_overflow_flags_context_truncated(self):
        # Bounded truncation alone cannot fit an enormous many-keyed bible, so
        # the fallback must explicitly flag the digest as truncated rather than
        # silently dropping keys or emitting an over-limit payload.
        compact=NovelService.compact_bible({f'field{i}':'值'*2000 for i in range(20)})
        self.assertTrue(compact.get('contextTruncated'))
        self.assertIsInstance(compact.get('facts'),str)
        self.assertLessEqual(len(json.dumps(compact,ensure_ascii=False)),14000)

    def test_review_recognizes_native_schema_registered_characters(self):
        bible={'protagonist':{'name':'林默','motivation':'复仇'},
               'mainCharacters':[{'name':'阿七','role':'书童'}],
               'supportingCast':[{'name':'老张'}]}
        good={'title':'x','chapterGoal':'目标','content':'一。\n\n二。',
              'stateChanges':[{'character':'林默','state':'进城'},
                              {'character':'阿七','state':'跟随'},
                              {'character':'老张','state':'守望'}]}
        result=review(good,bible,[])
        self.assertTrue(result['passed']); self.assertEqual(result['warnings'],[])
        bad={'title':'x','chapterGoal':'目标','content':'一。\n\n二。',
             'stateChanges':[{'character':'路人甲','state':'出现'}]}
        result2=review(bad,bible,[])
        self.assertIn('unregistered_character:路人甲',result2['warnings'])

    def test_review_still_blocks_conflicts_on_native_bible(self):
        bible={'protagonist':{'name':'林默'},
               'worldRules':[{'key':'magic','severity':'high'}],
               'timeline':[{'key':'city-burned'}],
               'foreshadowing':[{'key':'fan','status':'OPEN'}]}
        output={'title':'x','chapterGoal':'目标','content':'一。\n\n二。',
                'stateChanges':[{'rule':'fly'}],
                'eventsIntroduced':[{'key':'city-burned'}],
                'foreshadowingResolved':['other']}
        result=review(output,bible,[])
        self.assertFalse(result['passed'])
        self.assertIn('unauthorized_world_rule:fly',result['blockingIssues'])
        self.assertIn('timeline_event_redefinition:city-burned',result['blockingIssues'])
        self.assertIn('foreshadowing_not_open:other',result['blockingIssues'])

    def test_process_prompt_keeps_native_keys_and_reviews_against_native_bible(self):
        # End to end: a novel that registers its cast as protagonist/mainCharacters
        # (no template 'characters' key) must keep those facts in the prompt digest
        # AND pass review without a spurious unregistered_character warning.
        novel=self.store.create_novel('原生小说',{'protagonist':{'name':'林默'},'mainCharacters':[{'name':'阿七'}]})
        raw=json.loads(chapter_json()); raw['stateChanges']=[{'character':'阿七','state':'跟随'}]
        client=SequenceClient([json.dumps(raw,ensure_ascii=False)])
        job,_=self.store.create_job(novel['id'],1)
        service=NovelService(self.store,client,Config(data_dir=Path(self.tmp.name)),Path(__file__).parents[1])
        self.assertTrue(service.process(job))
        sent=json.loads(client.calls[0]); story_bible=sent['storyBible']
        self.assertEqual(story_bible['protagonist']['name'],'林默')
        self.assertEqual(story_bible['mainCharacters'][0]['name'],'阿七')
        self.assertNotIn('characters',story_bible)  # no phantom null template key
        chapter=self.store.chapter(novel['id'],1)
        review_raw=chapter['review']
        review_data=json.loads(review_raw) if isinstance(review_raw,str) else review_raw
        self.assertTrue(review_data['passed'])
        self.assertNotIn('unregistered_character:阿七',review_data['warnings'])

if __name__=='__main__': unittest.main()
