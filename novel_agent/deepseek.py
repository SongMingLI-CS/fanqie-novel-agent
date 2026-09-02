import json, time, urllib.request, urllib.error


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
                with self.opener(request,timeout=self.config.timeout) as response: data=json.loads(response.read().decode())
                message=data['choices'][0]['message']['content']; usage=data.get('usage',{})
                return message, {'model':self.config.model,'prompt_version':'novel-writer@1','input_tokens':usage.get('prompt_tokens',0),'output_tokens':usage.get('completion_tokens',0),'duration_ms':round((time.monotonic()-started)*1000),'request_status':'succeeded'}
            except Exception as exc:
                last=exc
                if attempt >= self.config.max_retries: break
                time.sleep(min(2 ** attempt, 8))
        raise DeepSeekError(f'DeepSeek request failed after retries: {type(last).__name__}')


def parse_output(text):
    try: return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find('{'), text.rfind('}')
        if start >= 0 and end > start:
            try: return json.loads(text[start:end+1])
            except json.JSONDecodeError: pass
        raise DeepSeekError('model returned invalid chapter JSON')
