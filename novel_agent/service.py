import json
import logging
import time
from pathlib import Path

from .deepseek import (
    DeepSeekError,
    parse_output,
    response_summary,
    validate_chapter_output,
)
from .exporters import export_chapter
from .prompts import PromptManager
from .reviewer import review
from .stages import AgentPipeline, StageAbort

logger=logging.getLogger(__name__)


class NovelService:
    def __init__(self, store, client, config, root=None, async_client=None, events=None):
        self.store = store
        self.client = client
        self.async_client = async_client
        self.events = events
        self.config = config
        self.root = Path(root or Path(__file__).parents[1])
        self.prompts = PromptManager(self.root)
        self._delta_buf = ""
        self._delta_since = 0.0

    # -- live event helpers ---------------------------------------------------

    def _emit(self, novel_id, run_id, event_type, payload=None):
        """Best-effort event publish; a failed event write never breaks generation."""
        if not self.events:
            return
        try:
            self.events.publish(novel_id, run_id, event_type, payload or {})
        except Exception:  # noqa: BLE001
            logger.debug("event publish failed type=%s", event_type)

    def _publish_delta(self, job, stage, text):
        if not self.events:
            return
        self._delta_buf += text
        now = time.monotonic()
        if len(self._delta_buf) >= 100 or (self._delta_buf and now - self._delta_since >= 0.3):
            self._flush_delta(job, stage)

    def _flush_delta(self, job, stage):
        if not self._delta_buf or not self.events:
            self._delta_buf = ""
            return
        self._emit(
            job["novel_id"], None, "llm.delta",
            {"chapter": job["chapter_number"], "stage": stage, "text": self._delta_buf},
        )
        self._delta_buf = ""
        self._delta_since = time.monotonic()

    def _publish_content(self, job, output):
        """Publish the validated prose so the console can type it out live."""
        if self.events and isinstance(output, dict) and output.get("content"):
            self._emit(
                job["novel_id"], None, "llm.text",
                {"chapter": job["chapter_number"], "text": output["content"]},
            )
    def context(self,nid,number):
        novel=self.store.get_novel(nid); recent=self.store.recent(nid,before=number); bible=self.compact_bible(novel['story_bible'])
        refs=self.root/'.agents/skills/novel-writer/references'; skill='\n'.join((refs/x).read_text(encoding='utf-8') for x in ('story-bible.md','style-rules.md','chapter-template.md','review-rubric.md'))
        return novel,recent,bible,skill

    @staticmethod
    def compact_bible(bible, limit=14000):
        """Keep the prompt context bounded while retaining every native bible fact.

        Earlier versions whitelisted a fixed template key set, silently dropping
        the keys real novels actually use (``protagonist``, ``mainCharacters``,
        ``storyArcs``, ...) and injecting nulls that starved the model of
        role/arc context. Every top-level key now survives with bounded
        truncation; only an extreme over-budget bible degrades to a truncated
        facts blob flagged ``contextTruncated`` (a budget overflow, never a
        silent key filter).
        """
        if not isinstance(bible, dict):
            return {}

        def _bounded(value):
            if isinstance(value, list):
                return [_bounded(item) for item in value[:20]]
            if isinstance(value, dict):
                return {key: _bounded(child) for key, child in value.items()}
            if isinstance(value, str):
                return value[:2000]
            return value

        compact = {key: _bounded(value) for key, value in bible.items()}
        encoded = json.dumps(compact, ensure_ascii=False)
        return compact if len(encoded) <= limit else {'contextTruncated': True, 'facts': encoded[:max(0, limit - 100)]}
    def process(self, job):
        novel, recent, bible, skill = self.context(job['novel_id'], job['chapter_number'])
        self.store.set_status(job['novel_id'], job['chapter_number'], 'PLANNING')
        if novel['paused']:
            self.store.set_status(job['novel_id'], job['chapter_number'], 'CANCELLED')
            self.store.db.execute(
                "UPDATE jobs SET status='CANCELLED',error=? WHERE id=?",
                ('novel_paused', job['id']),
            )
            self.store.db.commit()
            return False

        run_config = self._run_config(job)
        stages = run_config['stages']
        if stages:
            return AgentPipeline(self).run(
                job, stages, run_config['targetWords'], run_config['autoExportTxt'],
                novel, recent, bible, skill,
            )
        return self._compose(job, novel, recent, bible, skill, run_config['targetWords'])

    def _run_config(self, job):
        """Resolve the per-run generation config from the job, else the env default."""
        cfg = {}
        raw = job.get('config') or ''
        if raw:
            try:
                parsed = json.loads(raw) if isinstance(raw, str) else raw
            except (TypeError, ValueError):
                parsed = {}
            if isinstance(parsed, dict):
                cfg = parsed
        stages = cfg.get('stages')
        if stages is None:
            stages = list(self.config.agent_stages or [])
        stages = [s for s in stages if s in ('outline', 'chapter', 'polish')]
        if stages and 'chapter' not in stages:
            stages = ['chapter']  # a draft stage is mandatory
        try:
            target_words = int(cfg.get('targetWords') or 0)
        except (TypeError, ValueError):
            target_words = 0
        auto_export = bool(cfg.get('autoExportTxt', self.config.auto_export_txt))
        return {'stages': stages, 'targetWords': target_words, 'autoExportTxt': auto_export}

    def complete_structured(self, job, system, user, validate, ctx):
        """One structured call, with a single bounded repair retry on bad JSON.

        Raises :class:`StageAbort` after recording usage and scheduling the job
        for retry/failure. Non-``DeepSeekError`` exceptions (e.g. a code-level
        crash) intentionally propagate so the worker can mark the job crashed.
        """
        json_failure = False
        first_failure = ''
        try:
            raw, usage = self.client.complete(system, user)
            try:
                output = validate(parse_output(raw), ctx)
            except DeepSeekError:
                json_failure = True
                first_failure = response_summary(raw)
                repair = self.prompts.render('repair', {})
                raw, usage = self.client.complete(system, user + '\n' + repair)
                output = validate(parse_output(raw), ctx)
        except DeepSeekError as exc:
            detail = str(exc)
            if json_failure:
                detail += '; first_response_summary=' + first_failure
            self.store.record_usage(
                job, self.config.model, self.prompts.version, 'failed', detail
            )
            retry = self.store.fail_job(
                job, detail, 0 if json_failure else self.config.max_job_attempts
            )
            raise StageAbort(retry) from exc
        usage = {**usage, 'prompt_version': self.prompts.version}
        return output, raw, usage
    # -- streaming (async) entry point ---------------------------------------

    async def process_stream(self, job):
        """Async orchestrator mirroring :meth:`process`, publishing live events.

        Uses the configured async client so the worker can forward token deltas
        to the ``events`` table while DeepSeek is still generating.
        """
        if self.async_client is None:
            raise RuntimeError("NovelService is not configured with an async client")
        nid, number = job["novel_id"], job["chapter_number"]
        novel, recent, bible, skill = self.context(nid, number)
        self.store.set_status(nid, number, "PLANNING")
        self._emit(nid, None, "chapter.status", {"chapter": number, "status": "PLANNING"})
        if novel["paused"]:
            self.store.set_status(nid, number, "CANCELLED")
            self.store.db.execute(
                "UPDATE jobs SET status='CANCELLED',error=? WHERE id=?",
                ("novel_paused", job["id"]),
            )
            self.store.db.commit()
            self._emit(nid, None, "chapter.status", {"chapter": number, "status": "CANCELLED"})
            return False
        run_config = self._run_config(job)
        stages = run_config["stages"]
        if stages:
            return await AgentPipeline(self).run_async(
                job, stages, run_config["targetWords"], run_config["autoExportTxt"],
                novel, recent, bible, skill,
            )
        return await self._compose_stream(
            job, novel, recent, bible, skill,
            run_config["targetWords"], run_config["autoExportTxt"],
        )

    async def _stream_collect(self, job, system, user, stage):
        """Consume the async token stream, publishing throttled deltas."""
        parts = []
        usage = {}
        async for chunk in self.async_client.stream(system, user):
            if chunk["type"] == "delta":
                parts.append(chunk["text"])
                self._publish_delta(job, stage, chunk["text"])
            elif chunk["type"] == "usage":
                usage = chunk["usage"]
        self._flush_delta(job, stage)
        text = "".join(parts)
        if not text.strip():
            raise DeepSeekError("DeepSeek returned empty message.content", "empty_content")
        return text, usage

    async def complete_structured_async(self, job, system, user, validate, ctx, stage=None):
        """Async twin of :meth:`complete_structured` with live token events."""
        json_failure = False
        first_failure = ""
        try:
            text, usage = await self._stream_collect(job, system, user, stage)
            try:
                output = validate(parse_output(text), ctx)
            except DeepSeekError:
                json_failure = True
                first_failure = response_summary(text)
                repair = self.prompts.render("repair", {})
                text, usage = await self._stream_collect(job, system, user + "\n" + repair, stage)
                output = validate(parse_output(text), ctx)
        except DeepSeekError as exc:
            detail = str(exc)
            if json_failure:
                detail += "; first_response_summary=" + first_failure
            self.store.record_usage(
                job, self.config.model, self.prompts.version, "failed", detail
            )
            retry = self.store.fail_job(
                job, detail, 0 if json_failure else self.config.max_job_attempts
            )
            raise StageAbort(retry) from exc
        usage = {**usage, "prompt_version": self.prompts.version}
        return output, text, usage

    async def _compose_stream(self, job, novel, recent, bible, skill, target_words, auto_export):
        system = self.prompts.system()
        request = self.prompts.render("request", {})
        prompt = json.dumps({
            "skill": skill,
            "storyBible": bible,
            "recentChapterSummaries": [x.get("summary", "") for x in recent],
            "chapterNumber": job["chapter_number"],
            "request": request,
        }, ensure_ascii=False)
        nid, number = job["novel_id"], job["chapter_number"]
        self.store.set_status(nid, number, "GENERATING")
        self._emit(nid, None, "chapter.status", {"chapter": number, "status": "GENERATING"})

        def _validate(value, _ctx):
            return validate_chapter_output(value, number)

        try:
            output, raw, usage = await self.complete_structured_async(
                job, system, prompt, _validate, None, stage="chapter"
            )
        except StageAbort:
            return False
        self._publish_content(job, output)
        return await self._finalize_stream(
            job, output, raw, usage, novel, recent, bible, target_words, auto_export
        )

    async def _finalize_stream(self, job, output, raw, usage, novel, recent, bible, target_words, auto_export):
        passed = self.finalize(
            job, output, raw, usage, novel, recent, bible, target_words, auto_export
        )
        chapter = self.store.chapter(job["novel_id"], job["chapter_number"])
        self._emit(job["novel_id"], None, "chapter.ready", {
            "chapter": job["chapter_number"],
            "status": chapter.get("status") if chapter else "FAILED",
            "passed": bool(passed),
        })
        return passed

    def _compose(self, job, novel, recent, bible, skill, target_words):
        system = self.prompts.system()
        request = self.prompts.render('request', {})
        prompt = json.dumps({
            'skill': skill,
            'storyBible': bible,
            'recentChapterSummaries': [x.get('summary', '') for x in recent],
            'chapterNumber': job['chapter_number'],
            'request': request,
        }, ensure_ascii=False)
        self.store.set_status(job['novel_id'], job['chapter_number'], 'GENERATING')

        def _validate(value, _ctx):
            return validate_chapter_output(value, job['chapter_number'])

        try:
            output, raw, usage = self.complete_structured(job, system, prompt, _validate, None)
        except StageAbort:
            return False
        return self.finalize(
            job, output, raw, usage, novel, recent, bible,
            target_words, self.config.auto_export_txt,
        )

    def finalize(self, job, output, raw, usage, novel, recent, bible, target_words, auto_export):
        nid, number = job['novel_id'], job['chapter_number']
        self.store.set_status(nid, number, 'REVIEWING')
        rules = bible.get('styleRules', {}) if isinstance(bible, dict) else {}
        default_target = rules.get('chapterLength', 0) if isinstance(rules, dict) else 0
        target = int(target_words) if target_words else (int(default_target) if isinstance(default_target, int) else 0)
        # Consistency checks run against the authoritative native bible (all keys,
        # untruncated) so registered native-schema characters and canon facts are
        # never misread because they happened to fall outside the prompt digest.
        result = review(output, novel['story_bible'], recent, target)
        proposed = {
            'currentChapter': number,
            'stateChanges': output.get('stateChanges', []),
            'events': output.get('eventsIntroduced', []),
            'foreshadowingResolved': output.get('foreshadowingResolved', []),
        }
        self.store.save_generation(job, output, raw, result, usage, proposed)
        if result['passed'] and auto_export:
            chapter = self.store.chapter(nid, number)
            try:
                path = export_chapter(chapter, novel, 'txt', self.config.data_dir / 'exports')
                logger.info('automatic draft txt exported chapter=%s path=%s', number, path)
            except OSError as exc:
                logger.warning(
                    'automatic draft txt export failed chapter=%s error=%s',
                    number, type(exc).__name__,
                )
        return result['passed']
