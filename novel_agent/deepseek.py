import json, os, re, ssl, time, urllib.request, urllib.error


class DeepSeekError(Exception): pass


class DeepSeekClient:
    def __init__(self, config, opener=None): self.config=config; self.opener=opener or urllib.request.urlopen

    def complete(self, system, user):
        if not __import__('os').getenv('DEEPSEEK_API_KEY'): raise DeepSeekError('DEEPSEEK_API_KEY is not configured')
        if not self.config.base_url: raise DeepSeekError('DEEPSEEK_BASE_URL is not configured')
        body=json.dumps({'model':self.config.model,'messages':[{'role':'system','content':system},{'role':'user','content':user}], 'max_tokens':self.config.max_tokens, 'response_format':{'type':'json_object'}}).encode()
        request=urllib.request.Request(self.config.base_url.rstrip('/')+'/chat/completions',body,{'Authorization':'Bearer '+__import__('os').environ['DEEPSEEK_API_KEY'],'Content-Type':'application/json'})
        started=time.monotonic(); last=None
        for attempt in range(self.config.max_retries+1):
            try:
                context=ssl.create_default_context(cafile=self.config.ca_bundle or None)
                with self.opener(request,timeout=self.config.timeout,context=context) as response: data=json.loads(response.read().decode())
                message=data['choices'][0]['message']
                # R1 may include reasoning_content, but only content is the answer contract.
                if not isinstance(message, dict) or not isinstance(message.get('content'), str):
                    raise DeepSeekError('DeepSeek response missing message.content')
                message=message['content']; usage=data.get('usage',{})
                return message, {'model':self.config.model,'prompt_version':'novel-writer@1','input_tokens':usage.get('prompt_tokens',0),'output_tokens':usage.get('completion_tokens',0),'duration_ms':round((time.monotonic()-started)*1000),'request_status':'succeeded'}
            except Exception as exc:
                last=exc
                if attempt >= self.config.max_retries: break
                time.sleep(min(2 ** attempt, 8))
        raise DeepSeekError(f'DeepSeek request failed after retries: {type(last).__name__}')


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
