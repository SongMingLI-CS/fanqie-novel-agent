import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse
from .config import Config
from .store import Store
from .service import NovelService
from .deepseek import DeepSeekClient
from .exporters import export_chapter
from .auth import authorize

ROOT=__import__('pathlib').Path(__file__).parents[1]
config=Config(); store=Store(config.data_dir/'novel.sqlite3'); service=NovelService(store,DeepSeekClient(config),config,ROOT)

class Handler(BaseHTTPRequestHandler):
    def send_json(self, status, data):
        body=json.dumps(data,ensure_ascii=False).encode(); self.send_response(status); self.send_header('Content-Type','application/json; charset=utf-8'); self.send_header('Content-Length',str(len(body))); self.end_headers(); self.wfile.write(body)
    def body(self): return json.loads(self.rfile.read(int(self.headers.get('Content-Length',0)) or 0) or b'{}')
    def do_GET(self):
        p=urlparse(self.path).path; parts=p.strip('/').split('/')
        try:
            if p.startswith('/api/') and not authorize(self,config): return self.send_json(401,{'code':'unauthorized','message':'Authentication required','details':{}})
            if p=='/':
                body=(ROOT/'static/index.html').read_bytes(); self.send_response(200); self.send_header('Content-Type','text/html; charset=utf-8'); self.send_header('Content-Length',str(len(body))); self.end_headers(); self.wfile.write(body); return
            if p=='/api/novels': return self.send_json(200,[dict(x) for x in store.db.execute('SELECT * FROM novels ORDER BY updated_at DESC')])
            if len(parts)==3 and parts[:2]==['api','novels']:
                novel=store.get_novel(parts[2]); return self.send_json(200,novel) if novel else self.send_json(404,{'code':'not_found','message':'Novel not found','details':{}})
            if len(parts)==4 and parts[:2]==['api','novels'] and parts[3]=='chapters':
                if not store.get_novel(parts[2]): return self.send_json(404,{'code':'not_found','message':'Novel not found','details':{}})
                return self.send_json(200,store.chapters(parts[2]))
            if len(parts)==3 and parts[:2]==['api','jobs']:
                job=store.get_job(parts[2]); return self.send_json(200,job) if job else self.send_json(404,{'code':'not_found','message':'Job not found','details':{}})
            if len(parts)==4 and parts[:2]==['api','novels'] and parts[3]=='jobs': return self.send_json(200,store.jobs(parts[2]))
            if len(parts)==4 and parts[:2]==['api','novels'] and parts[3]=='usage': return self.send_json(200,store.usage(parts[2]))
            if len(parts)==3 and parts[:2]==['api','chapters']:
                chapter=store.chapter_by_id(parts[2]); return self.send_json(200,chapter) if chapter else self.send_json(404,{'code':'not_found','message':'Chapter not found','details':{}})
            return self.send_json(404,{'code':'not_found','message':'Route not found','details':{}})
        except Exception: return self.send_json(500,{'code':'internal_error','message':'Request failed','details':{}})
    def do_POST(self):
        p=urlparse(self.path).path; parts=p.strip('/').split('/'); data=self.body()
        try:
            if p.startswith('/api/') and not authorize(self,config): return self.send_json(401,{'code':'unauthorized','message':'Authentication required','details':{}})
            if p=='/api/novels':
                if not str(data.get('title','')).strip(): raise ValueError('title_required')
                return self.send_json(201,store.create_novel(data['title'],data.get('storyBible',{}),data.get('genre',''),data.get('volume','')))
            if len(parts)==5 and parts[:2]==['api','novels'] and parts[3]=='chapters' and parts[4]=='generate':
                job,created=store.create_job(parts[2],int(data.get('chapterNumber') or store.get_novel(parts[2])['current_chapter']+1)); return self.send_json(202 if created else 200,job)
            if len(parts)==4 and parts[:2]==['api','jobs'] and parts[3]=='cancel': store.cancel_job(parts[2]); return self.send_json(200,store.get_job(parts[2]))
            if len(parts)==4 and parts[:2]==['api','novels'] and parts[3]=='continue':
                store.set_paused(parts[2],False)
                novel=store.get_novel(parts[2]); job,created=store.create_job(parts[2],novel['current_chapter']+1)
                return self.send_json(202,{'job':job,'created':created,'requestedCount':max(1,int(data.get('count',1))),'note':'每章人工确认后再次调用 continue 才会创建下一章'})
            if len(parts)==4 and parts[:3]==['api','chapters'] and parts[3]=='export':
                ch=store.chapter(data['novelId'],int(data['chapterNumber']))
                fmt=data.get('format','txt'); export_job,created=store.create_export_job(ch,fmt) if ch else (None,False)
                if export_job and not created and export_job['status']=='SUCCEEDED': return self.send_json(200,{'path':export_job['path'],'status':'EXPORTED','idempotent':True})
                if not ch or not ch['review'].get('passed') or ch['status'] not in ('DRAFT_READY','WAITING_APPROVAL'):
                    raise ValueError('chapter_not_ready_for_export')
                path=export_chapter(ch,store.get_novel(data['novelId']),fmt,config.data_dir/'exports');
                store.complete_export_job(export_job,path)
                store.record_export(data['novelId'],ch['number'])
                return self.send_json(200,{'path':str(path),'status':'EXPORTED','idempotent':ch['status']=='EXPORTED'})
            if len(parts)==4 and parts[:3]==['api','chapters'] and parts[3] in ('manual-publish','publish'):
                if not str(data.get('platform','')).strip() or not str(data.get('operator','')).strip(): raise ValueError('platform_and_operator_required')
                store.manual_publish(data['novelId'],int(data['chapterNumber']),data); return self.send_json(200,store.chapter(data['novelId'],int(data['chapterNumber'])))
            if len(parts)==4 and parts[:2]==['api','chapters'] and parts[3]=='review':
                ch=store.chapter_by_id(parts[2])
                if not ch: return self.send_json(404,{'code':'not_found','message':'Chapter not found','details':{}})
                result=__import__('novel_agent.reviewer',fromlist=['review']).review(ch,store.get_novel(ch['novel_id'])['story_bible'],store.recent(ch['novel_id'])); store.db.execute("UPDATE chapters SET review=?,status=?,updated_at=? WHERE id=?",(json.dumps(result), 'WAITING_APPROVAL' if result['passed'] else 'FAILED',__import__('novel_agent.store',fromlist=['now']).now(),parts[2])); store.db.commit(); return self.send_json(200,result)
            if len(parts)==4 and parts[:2]==['api','chapters'] and parts[3]=='approve':
                ch=store.chapter_by_id(parts[2]);
                if not ch or ch['review'].get('blockingIssues'): raise ValueError('chapter cannot be approved')
                store.set_status(ch['novel_id'],ch['number'],'DRAFT_READY'); return self.send_json(200,store.chapter_by_id(parts[2]))
            if len(parts)==4 and parts[:3]==['api','novels'] and parts[3] in ('pause','continue'): store.set_paused(parts[2],parts[3]=='pause'); return self.send_json(200,store.get_novel(parts[2]))
            return self.send_json(404,{'code':'not_found','message':'Route not found','details':{}})
        except (ValueError,KeyError) as exc: return self.send_json(400,{'code':'invalid_request','message':str(exc),'details':{}})
        except Exception: return self.send_json(500,{'code':'internal_error','message':'Request failed','details':{}})
    def do_PATCH(self):
        p=urlparse(self.path).path; parts=p.strip('/').split('/'); data=self.body()
        try:
            if p.startswith('/api/') and not authorize(self,config): return self.send_json(401,{'code':'unauthorized','message':'Authentication required','details':{}})
            if len(parts)==4 and parts[:2]==['api','novels'] and parts[3]=='story-bible':
                version=store.update_bible(parts[2],data.get('storyBible',data)); return self.send_json(200,{'version':version,'novel':store.get_novel(parts[2])})
            if len(parts)==3 and parts[:2]==['api','chapters']:
                return self.send_json(200,store.update_draft(parts[2],data))
            return self.send_json(404,{'code':'not_found','message':'Route not found','details':{}})
        except Exception: return self.send_json(500,{'code':'internal_error','message':'Request failed','details':{}})
    def log_message(self,*args): pass

def main(): ThreadingHTTPServer(('127.0.0.1',8787),Handler).serve_forever()
if __name__=='__main__': main()
