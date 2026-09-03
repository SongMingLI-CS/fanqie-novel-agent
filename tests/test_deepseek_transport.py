import json
import os
import socket
import ssl
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from novel_agent.config import Config
from novel_agent.deepseek import DeepSeekClient, DeepSeekError
from novel_agent.service import NovelService
from novel_agent.store import Store


class BufferedResponse:
    def __init__(self, body=b'', status=200, headers=None):
        self.body=body; self.status=status; self.headers=headers or {}
    def __enter__(self): return self
    def __exit__(self,*args): pass
    def read(self): return self.body


class StreamResponse(BufferedResponse):
    def __init__(self, lines, status=200, headers=None):
        super().__init__(b'',status,headers); self.lines=iter(lines)
    def readline(self): return next(self.lines,b'')


def stream_for(content='{"ok":true}'):
    payload=json.dumps({'choices':[{'delta':{'content':content}}]}).encode()
    usage=json.dumps({'choices':[{'delta':{}}],'usage':{'prompt_tokens':2,'completion_tokens':3}}).encode()
    return [b'data: '+payload+b'\n',b'\n',b'data: '+usage+b'\n',b'\n',b'data: [DONE]\n']


class DeepSeekTransportTests(unittest.TestCase):
    def setUp(self):
        self.old_key=os.environ.get('DEEPSEEK_API_KEY'); os.environ['DEEPSEEK_API_KEY']='test-secret-key'
        self.config=Config(base_url='https://api.deepseek.com',model='test-model',max_retries=0,timeout=180,connect_timeout=10,max_tokens=32)
    def tearDown(self):
        if self.old_key is None: os.environ.pop('DEEPSEEK_API_KEY',None)
        else: os.environ['DEEPSEEK_API_KEY']=self.old_key

    def test_minimal_authenticated_stream_succeeds(self):
        client=DeepSeekClient(self.config,lambda *a,**k: StreamResponse(stream_for()))
        text,usage=client.complete('JSON only','Return ok')
        self.assertEqual(json.loads(text),{'ok':True}); self.assertEqual(usage['input_tokens'],2)

    def test_http_401_and_400_are_classified(self):
        for status,category in ((401,'http_401'),(400,'http_400')):
            with self.subTest(status=status):
                client=DeepSeekClient(self.config,lambda *a,s=status,**k: BufferedResponse(status=s))
                with self.assertRaises(DeepSeekError) as caught: client.complete('s','u')
                self.assertEqual(caught.exception.category,category)

    def test_http_429_honors_retry_after_and_recovers(self):
        responses=[BufferedResponse(status=429,headers={'Retry-After':'0'}),StreamResponse(stream_for())]
        config=Config(base_url=self.config.base_url,model='test-model',max_retries=1,timeout=180)
        client=DeepSeekClient(config,lambda *a,**k: responses.pop(0))
        text,_=client.complete('s','u'); self.assertEqual(json.loads(text),{'ok':True}); self.assertEqual(responses,[])

    def test_dns_tls_and_tcp_failures_are_classified(self):
        cases=((socket.gaierror('dns'),'dns_error'),(ssl.SSLError('tls'),'tls_error'),(ConnectionRefusedError('tcp'),'tcp_connection_error'))
        for error,category in cases:
            with self.subTest(category=category): self.assertEqual(DeepSeekClient._classified_error(error).category,category)

    def test_connect_and_first_byte_timeouts_are_classified(self):
        class ConnectTimeout:
            def __init__(self,*a,**k): pass
            def connect(self): raise socket.timeout()
            def close(self): pass
        client=DeepSeekClient(self.config)
        with patch('novel_agent.deepseek.socket.getaddrinfo',return_value=[object()]),patch('novel_agent.deepseek.http.client.HTTPSConnection',ConnectTimeout):
            with self.assertRaises(DeepSeekError) as caught: client.complete('s','u')
        self.assertEqual(caught.exception.category,'connect_timeout')

        class Sock:
            def settimeout(self,value): pass
        class FirstByteTimeout:
            def __init__(self,*a,**k): self.sock=Sock()
            def connect(self): pass
            def request(self,*a,**k): pass
            def getresponse(self): raise socket.timeout()
            def close(self): pass
        with patch('novel_agent.deepseek.socket.getaddrinfo',return_value=[object()]),patch('novel_agent.deepseek.http.client.HTTPSConnection',FirstByteTimeout):
            with self.assertRaises(DeepSeekError) as caught: client.complete('s','u')
        self.assertEqual(caught.exception.category,'first_byte_timeout')

    def test_stream_interruption_and_read_timeout_are_classified(self):
        interrupted=DeepSeekClient(self.config,lambda *a,**k: StreamResponse(stream_for()[:-1]))
        with self.assertRaises(DeepSeekError) as caught: interrupted.complete('s','u')
        self.assertEqual(caught.exception.category,'stream_interrupted')

        class TimedOut(StreamResponse):
            def readline(self): raise socket.timeout()
        timed=DeepSeekClient(self.config,lambda *a,**k: TimedOut([]))
        with self.assertRaises(DeepSeekError) as caught: timed.complete('s','u')
        self.assertEqual(caught.exception.category,'first_byte_timeout')

        class MidStreamTimeout(StreamResponse):
            def __init__(self): self.calls=0; super().__init__([])
            def readline(self):
                self.calls+=1
                if self.calls==1: return b'data: {"choices":[{"delta":{"content":"{"}}]}\n'
                raise socket.timeout()
        midstream=DeepSeekClient(self.config,lambda *a,**k: MidStreamTimeout())
        with self.assertRaises(DeepSeekError) as caught: midstream.complete('s','u')
        self.assertEqual(caught.exception.category,'stream_read_timeout')

    def test_overall_timeout_is_classified(self):
        with self.assertRaises(DeepSeekError) as caught:
            DeepSeekClient._read_stream(StreamResponse([]),time.monotonic()-1)
        self.assertEqual(caught.exception.category,'overall_timeout')

    def test_truncated_completed_stream_is_rejected(self):
        event=json.dumps({'choices':[{'delta':{'content':'{"partial":'},'finish_reason':'length'}]}).encode()
        client=DeepSeekClient(self.config,lambda *a,**k: StreamResponse([b'data: '+event+b'\n',b'data: [DONE]\n']))
        with self.assertRaises(DeepSeekError) as caught: client.complete('s','u')
        self.assertEqual(caught.exception.category,'output_truncated')

    def test_failure_logs_never_include_api_key(self):
        client=DeepSeekClient(self.config,lambda *a,**k: BufferedResponse(status=401))
        with self.assertLogs('novel_agent.deepseek',level='INFO') as logs:
            with self.assertRaises(DeepSeekError): client.complete('secret prompt','secret response')
        combined='\n'.join(logs.output)
        self.assertNotIn(os.environ['DEEPSEEK_API_KEY'],combined); self.assertNotIn('secret prompt',combined)

    def test_incomplete_stream_does_not_persist_chapter_or_bible(self):
        class BrokenClient:
            def complete(self,*args): raise DeepSeekError('stream stopped','stream_interrupted')
        with tempfile.TemporaryDirectory() as directory:
            store=Store(Path(directory)/'db.sqlite3')
            novel=store.create_novel('测试',{'characters':[]}); job,_=store.create_job(novel['id'],1)
            service=NovelService(store,BrokenClient(),Config(data_dir=Path(directory),model='test-model'),Path(__file__).parents[1])
            self.assertFalse(service.process(job)); chapter=store.chapter(novel['id'],1)
            self.assertEqual(chapter['content'],''); self.assertEqual(chapter['raw_response'],'')
            self.assertEqual(store.get_novel(novel['id'])['story_bible_version'],1)
            self.assertEqual(store.db.execute('select count(*) from chapter_drafts').fetchone()[0],0)
            store.close()


if __name__=='__main__': unittest.main()
