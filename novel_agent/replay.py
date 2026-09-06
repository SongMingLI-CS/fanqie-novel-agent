"""Deterministic model capture/replay for keyless end-to-end demos.

The streaming pipeline normally talks to DeepSeek over httpx.  For a demo we
want the *exact* same code path (multi-agent stages, live events, SSE, the
typewriter console) to run with no API key and no network.  Two helpers live
here:

* :class:`CapturingAsyncClient` wraps a real async LLM client while a
  recording run happens, saving every ``(system, user)`` prompt together with
  the complete returned text and usage.
* :class:`ReplayClient` serves those calls back later in the same order and
  can be used as both the sync and the async client of a :class:`NovelService`.

``Recording`` is the JSON document format shared by both ends
(:meth:`Recording.save` / :meth:`Recording.load`).  A built-in demo recording
is available through :func:`default_recording`, so ``python -m novel_agent.demo
replay`` works out of the box without ever needing ``DEEPSEEK_API_KEY``.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

FORMAT = "novel-agent-replay/v1"


class ReplayError(ValueError):
    """Raised when a replay cannot serve the current model call."""


def _demo_usage():
    return {
        "model": "demo-builtin",
        "input_tokens": 0,
        "output_tokens": 0,
        "duration_ms": 0,
        "request_status": "succeeded",
    }


def _outline_json(number=1):
    return json.dumps({
        "chapterNumber": number,
        "title": "异界第一课：变量即灵力",
        "chapterGoal": "引入主角与核心设定",
        "beats": [
            {"goal": "主角穿越并觉醒感知", "detail": "以代码思维理解灵气"},
            {"goal": "遭遇第一处冲突", "detail": "灵力被他人夺走"},
        ],
        "charactersUsed": [],
        "eventsIntroduced": [],
        "foreshadowingAdded": [],
        "foreshadowingResolved": [],
        "stateChanges": [],
        "warnings": [],
    }, ensure_ascii=False)


def _chapter_json(number=1, content=""):
    return json.dumps({
        "chapterNumber": number,
        "title": "异界第一课：变量即灵力",
        "chapterGoal": "主角在异界立住脚跟",
        "summary": "阿零以代码思维首次驱动灵力，被神秘老者暗中观察。",
        "beats": [
            {"goal": "穿越后第一次呼吸", "detail": "发现天地灵气如数据流"},
            {"goal": "夺回被抢的灵力", "detail": "用变量与引用的思维反制"},
        ],
        "content": content,
        "charactersUsed": [],
        "eventsIntroduced": [],
        "foreshadowingAdded": [],
        "foreshadowingResolved": [],
        "stateChanges": [],
        "nextChapterHook": "老者推开了那扇刻满符文的门。",
        "warnings": [],
    }, ensure_ascii=False)


DEMO_BIBLE = {
    "mainline": "程序员阿零穿越异界，以代码思维破解灵力修行迷局。",
    "genre": "东方玄幻",
    "styleRules": {"chapterLength": 0},
}


def _demo_content():
    return (
        "夜色四合。山道尽头忽然亮起一盏青灯。\n\n"
        "阿零睁开眼，耳边全是细碎的嗡鸣——像是一台老旧的服务器在深夜里散热。"
        "他试着调用记忆里的函数，却只得到一片空白。这不是他熟悉的机房。\n\n"
        "他伸出手，指尖竟然真的飘起一行半透明的字符：err = 0。\n\n"
        "原来这方天地的灵气，在他眼里一直是结构化的数据流。\n\n"
        "不远处有人窃窃私语，说他是被废了灵根的杂役。阿零没有争辩，"
        "只是默默把那句\"废灵根\"当作一段待重构的旧代码。\n\n"
        "夜深之后，他盘膝坐下，试着把一个呼吸周期编译成最小可运行的循环。"
        "灵气沿着经脉流过，像内存被逐页点亮。\n\n"
        "突然，一只手按住了他的肩头。苍老的声音在耳边响起："
        "\"小子，你刚才那段'运行'，是谁教你的？\""
    )


DEMO_TITLE = "代号·异界编译器"
DEMO_VOLUME = "第一卷：初入异界"
DEMO_GENRE = "东方玄幻"
DEMO_STAGES = ["outline", "chapter", "polish"]

class Recording:
    """A replay document: novel snapshot + ordered list of model calls."""

    def __init__(self, meta=None, calls=None):
        self.meta = meta or {}
        self.calls = list(calls or [])

    # -- serialisation --------------------------------------------------------

    @classmethod
    def from_dict(cls, data):
        if not isinstance(data, dict) or data.get("format") != FORMAT:
            raise ReplayError("not a novel-agent replay file (format=novel-agent-replay/v1)")
        return cls(data.get("meta") or {}, data.get("calls") or [])

    def to_dict(self):
        return {"format": FORMAT, "meta": self.meta, "calls": self.calls}

    @classmethod
    def load(cls, path):
        try:
            with open(path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except OSError as exc:
            raise ReplayError(f"cannot read replay file {path}: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise ReplayError(f"replay file {path} is not valid JSON: {exc}") from exc
        return cls.from_dict(data)

    def save(self, path):
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.name}.tmp")
        try:
            temporary.write_text(
                json.dumps(self.to_dict(), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)
        return str(target)

    # -- convenience accessors ------------------------------------------------

    @property
    def novel(self):
        return self.meta.get("novel") or {}

    @property
    def stages(self):
        return list(self.meta.get("stages") or [])

    @property
    def chapter_number(self):
        return int(self.meta.get("chapterNumber") or 1)

    @property
    def strict(self):
        return bool(self.meta.get("strict", True))


def default_recording():
    """A self-contained demo recording (no key, no network required)."""
    meta = {
        "model": "demo-builtin",
        "recordedAt": "",
        "strict": False,  # built-in texts pair with this exact demo scenario
        "novel": {
            "title": DEMO_TITLE,
            "volume": DEMO_VOLUME,
            "genre": DEMO_GENRE,
            "storyBible": DEMO_BIBLE,
        },
        "stages": list(DEMO_STAGES),
        "chapterNumber": 1,
    }
    content = _demo_content()
    calls = [
        {"text": _outline_json(1), "usage": _demo_usage()},
        {"text": _chapter_json(1, content), "usage": _demo_usage()},
        {
            "text": _chapter_json(
                1,
                content
                + "\n\n山风掠过屋檐，那盏青灯又晃了晃。他知道，真正的编译才刚刚开始。",
            ),
            "usage": _demo_usage(),
        },
    ]
    return Recording(meta, calls)


def build_recording(novel, stages, chapter_number, calls, model=""):
    """Bundle a finished recording run into a :class:`Recording`."""
    meta = {
        "model": model,
        "recordedAt": datetime.now(timezone.utc).isoformat(),
        "strict": True,
        "novel": {
            "title": novel.get("title", ""),
            "volume": novel.get("volume", ""),
            "genre": novel.get("genre", ""),
            "storyBible": novel.get("story_bible", {}),
        },
        "stages": list(stages),
        "chapterNumber": int(chapter_number),
    }
    return Recording(meta, list(calls))

class CapturingAsyncClient:
    """Wrap an async client and append every finished call to ``capture``.

    ``capture`` receives one dict per model call: ``system``, ``user``,
    ``text`` (the fully streamed response) and ``usage``.  Real deltas are
    passed through untouched so a recording run behaves like a live run.
    """

    def __init__(self, inner, capture):
        self.inner = inner
        self.capture = capture

    async def aclose(self):
        await self.inner.aclose()

    async def stream(self, system, user):
        parts = []
        usage = {}
        async for chunk in self.inner.stream(system, user):
            if chunk["type"] == "delta":
                parts.append(chunk["text"])
            elif chunk["type"] == "usage":
                usage = chunk["usage"]
            yield chunk
        self.capture(
            {"system": system, "user": user, "text": "".join(parts), "usage": usage}
        )

    async def complete(self, system, user):
        parts = []
        usage = {}
        async for chunk in self.stream(system, user):
            if chunk["type"] == "delta":
                parts.append(chunk["text"])
            elif chunk["type"] == "usage":
                usage = chunk["usage"]
        return "".join(parts), usage


class ReplayClient:
    """Serve a :class:`Recording` back in call order, as sync + async client.

    Strict mode (the default for recorded files) verifies the incoming prompt
    matches the recorded one so a stale recording never silently answers a
    different scenario; pass ``loose=True`` to replay by order only (used by
    the built-in demo, whose scenario is fixed in code).
    """

    def __init__(self, recording, loose=None):
        self.recording = recording
        self.entries = list(recording.calls)
        self.calls = []
        self.loose = bool(recording.strict is False) if loose is None else loose

    def _next(self, system, user):
        if not self.entries:
            raise ReplayError(
                "replay exhausted: pipeline made more model calls than the recording has"
            )
        entry = self.entries.pop(0)
        self.calls.append({"system": system, "user": user})
        if not self.loose and "user" in entry:
            if entry.get("system") != system or entry.get("user") != user:
                raise ReplayError(
                    "replay mismatch: current prompt differs from the recorded call "
                    "(re-record this scenario, or pass --loose to replay by order only)"
                )
        text = entry.get("text", "")
        if not text.strip():
            raise ReplayError("recording contains an empty model response")
        return text, dict(entry.get("usage") or {})

    # -- sync client interface -------------------------------------------------

    def complete(self, system, user):
        return self._next(system, user)

    # -- async client interface -------------------------------------------------

    async def aclose(self):
        pass

    async def stream(self, system, user):
        text, usage = self._next(system, user)
        step = 6  # simulate token arrival; delta throttling merges these anyway
        for offset in range(0, len(text), step):
            yield {"type": "delta", "text": text[offset : offset + step]}
        yield {"type": "usage", "usage": usage}

    async def complete_async(self, system, user):
        parts = []
        usage = {}
        async for chunk in self.stream(system, user):
            if chunk["type"] == "delta":
                parts.append(chunk["text"])
            elif chunk["type"] == "usage":
                usage = chunk["usage"]
        return "".join(parts), usage
