import http.client, json, logging, os, re, socket, ssl, time, urllib.parse, urllib.request, urllib.error

logger=logging.getLogger(__name__)


class DeepSeekError(Exception): pass


class DeepSeekClient:
    def __init__(self, config, opener=None): self.config=config; self.opener=opener; self._injected=opener is not None

    def complete(self, system, user):
        if not __import__('os').getenv('DEEPSEEK_API_KEY'): raise DeepSeekError('DEEPSEEK_API_KEY is not configured')
        if not self.config.base_url: raise DeepSeekError('DEEPSEEK_BASE_URL is not configured')
        body=json.dumps({'model':self.config.model,'messages':[{'role':'system','content':system},{'role':'user','content':user}], 'max_tokens':self.config.max_tokens, 'stream':True, 'response_format':{'type':'json_object'}},ensure_ascii=False).encode()
        request=urllib.request.Request(self.config.base_url.rstrip('/')+'/chat/completions',body,{'Authorization':'Bearer '+os.environ['DEEPSEEK_API_KEY'],'Content-Type':'application/json'})
        started=time.monotonic(); last=None
        for attempt in range(self.config.max_retries+1):
            try:
                context=ssl.create_default_context(cafile=self.config.ca_bundle or None)
                logger.info('deepseek request start attempt=%s model=%s stream=true',attempt+1,self.config.model)
                data=self._request(request,body,context)
                message=data['choices'][0]['message']
                # R1 may include reasoning_content, but only content is the answer contract.
                if not isinstance(message, dict) or not isinstance(message.get('content'), str):
                    raise DeepSeekError('DeepSeek response missing message.content')
                message=message['content']; usage=data.get('usage',{})
                elapsed=round((time.monotonic()-started)*1000); logger.info('deepseek request succeeded status=200 duration_ms=%s',elapsed)
                return message, {'model':self.config.model,'prompt_version':'novel-writer@1','input_tokens':usage.get('prompt_tokens',0),'output_tokens':usage.get('completion_tokens',0),'duration_ms':elapsed,'request_status':'succeeded'}
            except Exception as exc:
                last=exc
                category='timeout' if isinstance(exc,(TimeoutError, socket.timeout)) else 'tls' if isinstance(exc,ssl.SSLError) else 'network' if isinstance(exc,(OSError,urllib.error.URLError)) else 'http_or_response'
                logger.warning('deepseek request failed category=%s phase=%s attempt=%s duration_ms=%s',category,'request',attempt+1,round((time.monotonic()-started)*1000))
                if attempt >= self.config.max_retries: break
                time.sleep(min(2 ** attempt, 8))
        raise DeepSeekError(f'DeepSeek request failed after retries: {type(last).__name__}')

    def _request(self, request, body, context):
        if self._injected:
            with self.opener(request,timeout=self.config.timeout,context=context) as response:
                status=getattr(response,'status',200); logger.info('deepseek http status=%s',status)
                return self._decode(response.read())
        parsed=urllib.parse.urlparse(request.full_url); host=parsed.hostname; port=parsed.port or 443
        try:
            socket.getaddrinfo(host,port,type=socket.SOCK_STREAM); logger.info('deepseek dns resolved host=%s',host)
        except socket.gaierror:
            logger.warning('deepseek dns failed host=%s',host); raise
        connection=http.client.HTTPSConnection(host,port,timeout=self.config.connect_timeout,context=context)
        try:
            connection.request('POST',parsed.path or '/',body,dict(request.header_items()))
            response=connection.getresponse(); logger.info('deepseek http status=%s',response.status)
            if response.status >= 400: raise DeepSeekError(f'DeepSeek HTTP {response.status}')
            if response.fp: response.fp.raw._sock.settimeout(self.config.timeout)
            return self._decode(response.read())
        finally: connection.close()

    @staticmethod
    def _decode(raw):
        text=raw.decode('utf-8') if isinstance(raw,bytes) else raw
        if text.lstrip().startswith('data:'):
            content=[]; final={};
            for line in text.splitlines():
                if not line.startswith('data:'): continue
                payload=line[5:].strip()
                if payload=='[DONE]': continue
                item=json.loads(payload); choice=(item.get('choices') or [{}])[0]; delta=choice.get('delta',{})
                if isinstance(delta.get('content'),str): content.append(delta['content'])
                if item.get('usage'): final['usage']=item['usage']
            return {'choices':[{'message':{'content':''.join(content)}}],**final}
        return json.loads(text)


def response_summary(text, limit=500):
    compact=' '.join(str(text).split())
    return compact[:limit] + ('…' if len(compact) > limit else '')


def parse_output(text):
    if not isinstance(text, str): raise DeepSeekError('model returned non-text chapter JSON')
    cleaned=text.strip()
    cleaned=re.sub(r'^```(?:json)?\s*', '', cleaned, count=1, flags=re.IGNORECASE)
    cleaned=re.sub(r'\s*```$', '', cleaned, count=1)
    try: return json.loads(cleaned)
    except json.JSONDecodeError:
        start, end = cleaned.find('{'), cleaned.rfind('}')
        if start >= 0 and end > start:
            try: return json.loads(cleaned[start:end+1])
            except json.JSONDecodeError: pass
        raise DeepSeekError('model returned invalid chapter JSON')


def validate_chapter_output(value, chapter_number):
    required=('chapterNumber','title','chapterGoal','beats','content','charactersUsed','eventsIntroduced','foreshadowingAdded','foreshadowingResolved','stateChanges','nextChapterHook','warnings')
    if not isinstance(value, dict): raise DeepSeekError('model chapter JSON must be an object')
    missing=[key for key in required if key not in value]
    if missing: raise DeepSeekError('model chapter JSON missing fields: '+','.join(missing))
    if value.get('chapterNumber') != chapter_number: raise DeepSeekError('chapter number mismatch')
    strings=('title','chapterGoal','content','nextChapterHook')
    if any(not isinstance(value[key], str) for key in strings): raise DeepSeekError('chapter text fields must be strings')
    arrays=tuple(key for key in required if key not in strings and key != 'chapterNumber')
    if any(not isinstance(value[key], list) for key in arrays): raise DeepSeekError('chapter collection fields must be arrays')
    if any(not value[key].strip() for key in ('title','chapterGoal','content')): raise DeepSeekError('chapter title, goal and content must be non-empty')
    return value
