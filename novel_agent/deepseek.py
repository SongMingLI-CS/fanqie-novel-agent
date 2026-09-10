import http.client, json, logging, os, re, socket, ssl, time, urllib.parse, urllib.request, urllib.error

from .prompts import VERSION as PROMPT_VERSION

logger=logging.getLogger(__name__)


class DeepSeekError(Exception):
    def __init__(self, message, category='response_error', status=None, retry_after=None):
        super().__init__(message)
        self.category=category; self.status=status; self.retry_after=retry_after


class DeepSeekClient:
    def __init__(self, config, opener=None): self.config=config; self.opener=opener; self._injected=opener is not None

    def complete(self, system, user):
        api_key=os.getenv('DEEPSEEK_API_KEY','')
        if not api_key: raise DeepSeekError('DEEPSEEK_API_KEY is not configured', 'configuration')
        if api_key != api_key.strip(): raise DeepSeekError('DEEPSEEK_API_KEY has surrounding whitespace', 'configuration')
        if not self.config.base_url: raise DeepSeekError('DEEPSEEK_BASE_URL is not configured')
        if not self.config.model: raise DeepSeekError('DEEPSEEK_MODEL is not configured')
        if self.config.thinking not in ('enabled','disabled'): raise DeepSeekError('DEEPSEEK_THINKING must be enabled or disabled','configuration')
        payload={'model':self.config.model,'messages':[{'role':'system','content':system},{'role':'user','content':user}], 'max_tokens':self.config.max_tokens, 'stream':True, 'stream_options':{'include_usage':True},'response_format':{'type':'json_object'},'thinking':{'type':self.config.thinking}}
        if self.config.thinking=='enabled': payload['reasoning_effort']=self.config.reasoning_effort
        body=json.dumps(payload,ensure_ascii=False).encode()
        request=urllib.request.Request(self.config.base_url.rstrip('/')+'/chat/completions',body,{'Authorization':'Bearer '+api_key,'Content-Type':'application/json'})
        started=time.monotonic(); last=None
        for attempt in range(self.config.max_retries+1):
            try:
                context=ssl.create_default_context(cafile=self.config.ca_bundle or None)
                host=urllib.parse.urlparse(request.full_url).hostname
                logger.info('deepseek request start host=%s attempt=%s model=%s thinking=%s stream=true',host,attempt+1,self.config.model,self.config.thinking)
                data=self._request(request,body,context)
                message=data['choices'][0]['message']
                # R1 may include reasoning_content, but only content is the answer contract.
                if not isinstance(message, dict) or not isinstance(message.get('content'), str):
                    raise DeepSeekError('DeepSeek response missing message.content')
                message=message['content']; usage=data.get('usage',{})
                if not message.strip(): raise DeepSeekError('DeepSeek returned empty message.content','empty_content')
                elapsed=round((time.monotonic()-started)*1000); logger.info('deepseek request succeeded status=200 duration_ms=%s',elapsed)
                return message, {'model':self.config.model,'prompt_version':PROMPT_VERSION,'input_tokens':usage.get('prompt_tokens',0),'output_tokens':usage.get('completion_tokens',0),'duration_ms':elapsed,'request_status':'succeeded'}
            except Exception as exc:
                last=exc if isinstance(exc,DeepSeekError) else self._classified_error(exc)
                logger.warning('deepseek request failed category=%s status=%s attempt=%s duration_ms=%s',last.category,last.status or '-',attempt+1,round((time.monotonic()-started)*1000))
                if attempt >= self.config.max_retries or last.category in ('configuration','http_400','http_401','http_404'): break
                delay=last.retry_after if last.retry_after is not None else min(2 ** attempt,8)
                time.sleep(max(0,min(delay,30)))
        raise DeepSeekError(f'DeepSeek request failed after retries: {last.category}',last.category,last.status)

    @staticmethod
    def _classified_error(exc):
        if isinstance(exc,ssl.SSLError): return DeepSeekError('DeepSeek TLS failure','tls_error')
        if isinstance(exc,socket.gaierror): return DeepSeekError('DeepSeek DNS failure','dns_error')
        if isinstance(exc,(TimeoutError,socket.timeout)): return DeepSeekError('DeepSeek request timeout','request_timeout')
        if isinstance(exc,(ConnectionError,OSError,urllib.error.URLError)): return DeepSeekError('DeepSeek TCP connection failure','tcp_connection_error')
        return DeepSeekError(f'DeepSeek response failure: {type(exc).__name__}','response_error')

    @staticmethod
    def _http_error(status, headers=None):
        retry_after=None
        if status==429 and headers:
            try: retry_after=float(headers.get('Retry-After',''))
            except (TypeError,ValueError): pass
        category={400:'http_400',401:'http_401',404:'http_404',429:'http_429'}.get(status,'http_error')
        return DeepSeekError(f'DeepSeek HTTP {status}',category,status,retry_after)

    def _request(self, request, body, context):
        if self._injected:
            with self.opener(request,timeout=self.config.timeout,context=context) as response:
                status=getattr(response,'status',200); logger.info('deepseek http status=%s',status)
                if status >= 400: raise self._http_error(status,getattr(response,'headers',None))
                if hasattr(response,'readline'): return self._read_stream(response,time.monotonic()+self.config.timeout)
                return self._decode(response.read())
        parsed=urllib.parse.urlparse(request.full_url); host=parsed.hostname; port=parsed.port or 443
        if parsed.scheme != 'https' or not host: raise DeepSeekError('DEEPSEEK_BASE_URL must be HTTPS','configuration')
        try:
            socket.getaddrinfo(host,port,type=socket.SOCK_STREAM); logger.info('deepseek dns resolved host=%s',host)
        except socket.gaierror as exc:
            logger.warning('deepseek dns failed host=%s',host); raise DeepSeekError('DeepSeek DNS failure','dns_error') from exc
        connection=http.client.HTTPSConnection(host,port,timeout=self.config.connect_timeout,context=context)
        try:
            try: connection.connect()
            except ssl.SSLError as exc: raise DeepSeekError('DeepSeek TLS failure','tls_error') from exc
            except (TimeoutError,socket.timeout) as exc: raise DeepSeekError('DeepSeek connection timeout','connect_timeout') from exc
            except OSError as exc: raise DeepSeekError('DeepSeek TCP connection failure','tcp_connection_error') from exc
            connection.sock.settimeout(self.config.timeout)
            connection.request('POST',parsed.path or '/',body,dict(request.header_items()))
            try: response=connection.getresponse()
            except (TimeoutError,socket.timeout) as exc: raise DeepSeekError('DeepSeek first byte timeout','first_byte_timeout') from exc
            logger.info('deepseek http status=%s',response.status)
            if response.status >= 400: raise self._http_error(response.status,response.headers)
            return self._read_stream(response,time.monotonic()+self.config.timeout)
        finally: connection.close()

    @staticmethod
    def _read_stream(response, deadline):
        content=[]; usage={}; finish_reason=None; saw_data=False; saw_done=False
        while True:
            remaining=deadline-time.monotonic()
            if remaining <= 0: raise DeepSeekError('DeepSeek overall request timeout','overall_timeout')
            sock=getattr(getattr(getattr(response,'fp',None),'raw',None),'_sock',None)
            if sock: sock.settimeout(remaining)
            try: raw_line=response.readline()
            except (TimeoutError,socket.timeout) as exc:
                category='stream_read_timeout' if saw_data else 'first_byte_timeout'
                raise DeepSeekError('DeepSeek stream timeout',category) from exc
            if not raw_line: break
            saw_data=True; line=raw_line.decode('utf-8') if isinstance(raw_line,bytes) else raw_line
            if not line.startswith('data:'): continue
            payload=line[5:].strip()
            if not payload: continue
            if payload=='[DONE]': saw_done=True; break
            try: item=json.loads(payload)
            except json.JSONDecodeError as exc: raise DeepSeekError('DeepSeek malformed stream event','stream_parse_error') from exc
            choice=(item.get('choices') or [{}])[0]; delta=choice.get('delta',{})
            # reasoning_content is deliberately ignored; only answer content is persisted.
            if isinstance(delta.get('content'),str): content.append(delta['content'])
            if choice.get('finish_reason') is not None: finish_reason=choice['finish_reason']
            if item.get('usage'): usage=item['usage']
        if not saw_done: raise DeepSeekError('DeepSeek stream interrupted before DONE','stream_interrupted')
        if finish_reason=='length': raise DeepSeekError('DeepSeek output truncated at max_tokens','output_truncated')
        return {'choices':[{'message':{'content':''.join(content)}}],'usage':usage}

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


def validate_outline_output(value, chapter_number):
    """Validate the outline stage output (a chapter plan without prose content)."""
    if not isinstance(value, dict): raise DeepSeekError('model outline JSON must be an object')
    if value.get('chapterNumber') != chapter_number: raise DeepSeekError('chapter number mismatch')
    if not isinstance(value.get('title'), str) or not value['title'].strip():
        raise DeepSeekError('outline title must be a non-empty string')
    if not isinstance(value.get('chapterGoal'), str) or not value['chapterGoal'].strip():
        raise DeepSeekError('outline chapterGoal must be a non-empty string')
    if not isinstance(value.get('beats'), list): raise DeepSeekError('outline beats must be an array')
    for key in ('charactersUsed','eventsIntroduced','foreshadowingAdded','foreshadowingResolved','stateChanges','warnings'):
        if value.get(key) is not None and not isinstance(value[key], list):
            raise DeepSeekError('outline '+key+' must be an array')
    return value
