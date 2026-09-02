import re
from collections import Counter


def review(output, bible, recent, target_words=0):
    issues=[]; warnings=[]; blocking=[]
    if not isinstance(output,dict):
        return {'passed':False,'score':0,'issues':[],'warnings':[],'blockingIssues':['invalid_structured_output'],'checkedAt':__import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat()}
    content=output.get('content','')
    if not output.get('title'): blocking.append('missing_title')
    if not content.strip(): blocking.append('missing_content')
    if '\n' not in content.strip() and len(content)>100: issues.append('paragraph_format')
    words=len(re.findall(r'\S',content))
    if target_words and abs(words-target_words)>target_words*.25: issues.append('word_count_out_of_range')
    sentences=re.findall(r'[^。！？!?]+[。！？!?]',content); duplicates=[x for x,n in Counter(sentences).items() if n>1 and len(x)>8]
    if duplicates: blocking.append('repeated_sentences')
    recent_text=''.join(c.get('content','') for c in recent); overlap=sum(1 for s in sentences if len(s)>12 and s in recent_text)
    if overlap: blocking.append('recent_chapter_overlap')
    if not output.get('chapterGoal'): blocking.append('missing_chapter_goal')
    if output.get('chapterGoal') and not content: blocking.append('goal_not_completed')
    for item in output.get('stateChanges',[]):
        if isinstance(item,dict) and item.get('character') and not any(item['character']==c.get('name') for c in bible.get('characters',[]) if isinstance(c,dict)): warnings.append('unregistered_character:'+item['character'])
        if isinstance(item,dict) and item.get('rule') and not any(item['rule']==r.get('key') for r in bible.get('worldRules',[]) if isinstance(r,dict)): blocking.append('unauthorized_world_rule:'+item['rule'])
    known_events={e.get('key') for e in bible.get('timeline',[]) if isinstance(e,dict)}
    for event in output.get('eventsIntroduced',[]):
        if isinstance(event,dict) and event.get('key') in known_events: blocking.append('timeline_event_redefinition:'+event['key'])
    open_foreshadowing={f.get('key') for f in bible.get('foreshadowing',[]) if isinstance(f,dict) and f.get('status','OPEN')=='OPEN'}
    for key in output.get('foreshadowingResolved',[]):
        value=key.get('key') if isinstance(key,dict) else key
        if value not in open_foreshadowing: blocking.append('foreshadowing_not_open:'+str(value))
    forbidden=bible.get('forbiddenContent',[])
    for term in forbidden:
        if term and term in content: blocking.append('forbidden_content:'+term)
    score=max(0,100-len(issues)*8-len(blocking)*20)
    return {'passed':not blocking and not any(x in issues for x in ('word_count_out_of_range','paragraph_format')),'score':score,'issues':issues,'warnings':warnings,'blockingIssues':blocking,'checkedAt':__import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat()}
