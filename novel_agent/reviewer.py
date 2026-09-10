import re
from collections import Counter

# CJK unified ideographs, extension A and compatibility ideographs.
_CJK = re.compile(r'[\u4e00-\u9fff\u3400-\u4dbf\uf900-\ufaff]')


def count_words(text):
    """Count text the way a Chinese web-novel word count is measured.

    Each CJK character counts as one "word"; each contiguous run of latin or
    digit characters (including hyphen/period/apostrophe-separated tokens)
    counts as one. Whitespace and punctuation are ignored, so a paragraph of
    Chinese prose is not under/over counted by splitting on spaces.
    """
    if not text:
        return 0
    cjk = len(_CJK.findall(text))
    latin = len(re.findall(r"[A-Za-z0-9]+(?:[.'-][A-Za-z0-9]+)*", _CJK.sub(' ', text)))
    return cjk + latin


def _walk(value):
    """Yield every dict node reachable from ``value``, including itself."""
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def _registered_names(bible):
    """Collect every name a story bible registers.

    Canonical bibles keep a ``characters`` list of dicts, but real novels also
    declare people as a ``protagonist`` dict and as ``mainCharacters`` /
    ``storyArcs`` lists of dicts. Instead of guessing key names, harvest the
    ``name`` field from any reachable object so a legitimately registered
    character never raises a spurious ``unregistered_character`` warning.
    """
    if not isinstance(bible, dict):
        return set()
    return {node.get('name') for node in _walk(bible) if isinstance(node.get('name'), str)}


# The ``chapters`` table persists a snake_case projection of the model's
# structured output (``goal`` / ``state_changes`` / ``foreshadowing_added`` ...)
# while :func:`review` consumes the model contract (``chapterGoal`` /
# ``stateChanges`` / ...). Re-reviewing a saved chapter therefore has to translate
# one into the other; feeding the raw row in made every manual re-review fail
# with a spurious ``missing_chapter_goal`` blocking issue.
_CHAPTER_ROW_ALIASES = {
    "goal": "chapterGoal",
    "hook": "nextChapterHook",
    "characters": "charactersUsed",
    "events": "eventsIntroduced",
    "foreshadowing_added": "foreshadowingAdded",
    "foreshadowing_resolved": "foreshadowingResolved",
    "state_changes": "stateChanges",
    "number": "chapterNumber",
}


def chapter_as_output(chapter):
    """Project a persisted chapter row into the reviewer's output contract.

    Only the aliased keys are rewritten; every other field is passed through so
    future checks can read extra columns without another mapping change.
    """
    if not isinstance(chapter, dict):
        return {}
    output = dict(chapter)
    for row_key, output_key in _CHAPTER_ROW_ALIASES.items():
        if row_key not in chapter:
            continue
        value = chapter[row_key]
        output.pop(row_key, None)
        if value is not None:
            output[output_key] = value
    return output


def review(output, bible, recent, target_words=0):
    issues=[]; warnings=[]; blocking=[]
    # A bible may omit a section or carry it as JSON null (e.g. when the story
    # bible uses a different field set). Treat any non-list section as empty so
    # review never crashes iterating over None. Consistency checks consume the
    # native bible keys directly (see _registered_names below).
    if not isinstance(bible, dict):
        bible = {}
    for _section in ('characters', 'worldRules', 'timeline', 'foreshadowing', 'forbiddenContent'):
        if not isinstance(bible.get(_section), list):
            bible[_section] = []
    if not isinstance(output,dict):
        return {'passed':False,'score':0,'issues':[],'warnings':[],'blockingIssues':['invalid_structured_output'],'checkedAt':__import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat()}
    content=output.get('content','')
    if not output.get('title'): blocking.append('missing_title')
    if not content.strip(): blocking.append('missing_content')
    if '\n' not in content.strip() and len(content)>100: issues.append('paragraph_format')
    words=count_words(content)
    if target_words and abs(words-target_words)>target_words*.25: issues.append('word_count_out_of_range')
    sentences=re.findall(r'[^。！？!?]+[。！？!?]',content); duplicates=[x for x,n in Counter(sentences).items() if n>1 and len(x)>8]
    if duplicates: blocking.append('repeated_sentences')
    recent_text=''.join(c.get('content','') for c in recent); overlap=sum(1 for s in sentences if len(s)>12 and s in recent_text)
    if overlap: blocking.append('recent_chapter_overlap')
    if not output.get('chapterGoal'): blocking.append('missing_chapter_goal')
    if output.get('chapterGoal') and not content: blocking.append('goal_not_completed')
    registered = _registered_names(bible)
    known_world_rules = {r.get('key') for r in bible.get('worldRules', []) if isinstance(r, dict) and isinstance(r.get('key'), str)}
    for item in output.get('stateChanges', []):
        if isinstance(item, dict) and item.get('character') and item['character'] not in registered:
            warnings.append('unregistered_character:' + item['character'])
        if isinstance(item, dict) and item.get('rule') and item['rule'] not in known_world_rules:
            blocking.append('unauthorized_world_rule:' + item['rule'])
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
