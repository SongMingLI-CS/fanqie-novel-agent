"""Multi-agent pipeline: outline -> chapter -> polish (Chain of Responsibility).

Each stage is a handler that (1) builds its prompt from the shared
:class:`AgentContext`, (2) asks the model for structured JSON, (3) validates it,
and (4) hands its output forward for the next stage. After every stage the
pipeline checkpoints the accumulated memory via :class:`CheckpointRepository`,
so an interrupted run resumes from the first unfinished stage instead of
starting over.
"""
import json
import logging

from .checkpoints import CheckpointRepository
from .context import AgentContext
from .deepseek import validate_chapter_output, validate_outline_output
from .models import RunStatus

logger = logging.getLogger(__name__)


class StageAbort(Exception):
    """A stage failed after retrying; ``retry`` reports whether the job will retry."""

    def __init__(self, retry):
        super().__init__("stage aborted")
        self.retry = retry


class Stage:
    """Base handler in the pipeline. Subclasses define a prompt and validator."""

    key = ""
    prompt_name = ""

    def __init__(self, service):
        self.service = service

    def build_mapping(self, ctx):  # pragma: no cover - abstract
        return {}

    def user_prompt(self, ctx):
        return self.service.prompts.render(self.prompt_name, self.build_mapping(ctx))

    def validate(self, value, ctx):  # pragma: no cover - abstract
        raise NotImplementedError


class OutlineStage(Stage):
    key = "outline"
    prompt_name = "outline"

    def build_mapping(self, ctx):
        return {
            "chapterNumber": ctx.chapter_number,
            "storyBible": json.dumps(ctx.bible, ensure_ascii=False),
            "recentChapterSummaries": json.dumps(
                [c.get("summary", "") for c in ctx.recent], ensure_ascii=False
            ),
        }

    def validate(self, value, ctx):
        return validate_outline_output(value, ctx.chapter_number)


class ChapterStage(Stage):
    key = "chapter"
    prompt_name = "chapter"

    def build_mapping(self, ctx):
        outline = ctx.get_result("outline") or {}
        return {
            "chapterNumber": ctx.chapter_number,
            "storyBible": json.dumps(ctx.bible, ensure_ascii=False),
            "recentChapterSummaries": json.dumps(
                [c.get("summary", "") for c in ctx.recent], ensure_ascii=False
            ),
            "outline": json.dumps(outline, ensure_ascii=False),
        }

    def validate(self, value, ctx):
        return validate_chapter_output(value, ctx.chapter_number)


class PolishStage(Stage):
    key = "polish"
    prompt_name = "polish"

    def build_mapping(self, ctx):
        draft = ctx.get_result("chapter") or {}
        return {
            "storyBible": json.dumps(ctx.bible, ensure_ascii=False),
            "draft": json.dumps(draft, ensure_ascii=False),
        }

    def validate(self, value, ctx):
        return validate_chapter_output(value, ctx.chapter_number)


_STAGE_FACTORIES = {
    "outline": OutlineStage,
    "chapter": ChapterStage,
    "polish": PolishStage,
}
class AgentPipeline:
    """Runs the configured stages for one chapter with checkpoint/resume."""

    def __init__(self, service):
        self.service = service

    def _make(self, name):
        return _STAGE_FACTORIES[name](self.service)

    def run(self, job, stages, target_words, auto_export, novel, recent, bible, skill):
        repo = CheckpointRepository(self.service.store)
        ctx = AgentContext(
            novel, recent, bible, skill, job["chapter_number"], target_words
        )

        run = repo.find_run(job["id"])
        if run is not None and run["status"] == RunStatus.SUCCEEDED:
            return True  # already finished in a prior attempt

        # New run, changed stage plan, or resume disabled -> start clean.
        resume_ok = (
            run is not None
            and run["stages"] == list(stages)
            and self.service.config.auto_resume
        )
        if run is None or not resume_ok:
            run = repo.create_run(job, stages, target_words, auto_export)
            start = stages[0] if stages else None
        else:
            memory = repo.latest_memory(run["id"])
            ctx.load_memory(memory)
            start = repo.resume_from(run, stages)

        last_raw = ""
        last_usage = None
        if start is not None:
            for stage in stages[stages.index(start):]:
                stage_obj = self._make(stage)
                repo.mark_stage_started(run["id"], stage)
                try:
                    output, raw, usage = self.service.complete_structured(
                        job,
                        self.service.prompts.system(),
                        stage_obj.user_prompt(ctx),
                        stage_obj.validate,
                        ctx,
                    )
                except StageAbort as abort:
                    repo.mark_stage_failed(run["id"], stage, "stage aborted")
                    if not abort.retry:
                        repo.mark_run_failed(run["id"], "stage failed terminally")
                    return False
                ctx.set_result(stage, output)
                repo.mark_stage_done(run["id"], stage, output, ctx.memory_json(), raw)
                last_raw, last_usage = raw, usage

        # The final writing stage (polish if present, else chapter) is the result.
        final_output = None
        final_stage = None
        for name in reversed(stages):
            if name in ("chapter", "polish"):
                final_output = ctx.get_result(name)
                final_stage = name
                break
        if final_output is None:
            repo.mark_run_failed(run["id"], "no draft stage produced output")
            return False

        if last_usage is None:
            # Crash between last stage and finalize: synthesize a usage record.
            last_raw = repo.stage_raw(run["id"], final_stage)
            last_usage = {
                "model": self.service.config.model,
                "prompt_version": self.service.prompts.version,
                "request_status": "succeeded",
                "input_tokens": 0,
                "output_tokens": 0,
                "duration_ms": 0,
            }

        passed = self.service.finalize(
            job, final_output, last_raw, last_usage,
            novel, recent, bible, target_words, auto_export,
        )
        repo.mark_run_succeeded(run["id"])
        return passed

    async def run_async(self, job, stages, target_words, auto_export, novel, recent, bible, skill):
        """Async pipeline twin of :meth:`run`, publishing live progress events.

        Each stage transition, token stream (throttled in the service) and
        checkpoint write is mirrored into the ``events`` table so the web
        console can animate the timeline and type out the prose while DeepSeek
        is still generating.
        """
        service = self.service
        repo = CheckpointRepository(service.store)
        emit = service._emit
        nid, number = job["novel_id"], job["chapter_number"]
        ctx = AgentContext(novel, recent, bible, skill, number, target_words)

        run = repo.find_run(job["id"])
        if run is not None and run["status"] == RunStatus.SUCCEEDED:
            return True

        resume_ok = (
            run is not None
            and run["stages"] == list(stages)
            and service.config.auto_resume
        )
        if run is None or not resume_ok:
            run = repo.create_run(job, stages, target_words, auto_export)
            start = stages[0] if stages else None
            emit(nid, run["id"], "agent.run", {"chapter": number, "status": "RUNNING"})
        else:
            memory = repo.latest_memory(run["id"])
            ctx.load_memory(memory)
            start = repo.resume_from(run, stages)

        last_raw = ""
        last_usage = None
        if start is not None:
            for stage in stages[stages.index(start):]:
                stage_obj = self._make(stage)
                repo.mark_stage_started(run["id"], stage)
                emit(nid, run["id"], "agent.stage",
                     {"chapter": number, "stage": stage, "state": "running"})
                try:
                    output, raw, usage = await service.complete_structured_async(
                        job,
                        service.prompts.system(),
                        stage_obj.user_prompt(ctx),
                        stage_obj.validate,
                        ctx,
                        stage=stage,
                    )
                except StageAbort as abort:
                    repo.mark_stage_failed(run["id"], stage, "stage aborted")
                    emit(nid, run["id"], "agent.stage",
                         {"chapter": number, "stage": stage, "state": "failed"})
                    if not abort.retry:
                        repo.mark_run_failed(run["id"], "stage failed terminally")
                        emit(nid, run["id"], "agent.run",
                             {"chapter": number, "status": "FAILED"})
                    return False
                ctx.set_result(stage, output)
                repo.mark_stage_done(run["id"], stage, output, ctx.memory_json(), raw)
                emit(nid, run["id"], "agent.stage",
                     {"chapter": number, "stage": stage, "state": "done"})
                emit(nid, run["id"], "checkpoint.saved",
                     {"chapter": number, "stage": stage})
                last_raw, last_usage = raw, usage

        final_output = None
        final_stage = None
        for name in reversed(stages):
            if name in ("chapter", "polish"):
                final_output = ctx.get_result(name)
                final_stage = name
                break
        if final_output is None:
            repo.mark_run_failed(run["id"], "no draft stage produced output")
            emit(nid, run["id"], "agent.run", {"chapter": number, "status": "FAILED"})
            return False

        if last_usage is None:
            last_raw = repo.stage_raw(run["id"], final_stage)
            last_usage = {
                "model": service.config.model,
                "prompt_version": service.prompts.version,
                "request_status": "succeeded",
                "input_tokens": 0,
                "output_tokens": 0,
                "duration_ms": 0,
            }

        service._publish_content(job, final_output)
        passed = service.finalize(
            job, final_output, last_raw, last_usage,
            novel, recent, bible, target_words, auto_export,
        )
        repo.mark_run_succeeded(run["id"])
        emit(nid, run["id"], "agent.run", {"chapter": number, "status": "SUCCEEDED"})
        chapter = service.store.chapter(nid, number)
        emit(nid, None, "chapter.ready", {
            "chapter": number,
            "status": chapter.get("status") if chapter else "FAILED",
            "passed": bool(passed),
        })
        return passed
