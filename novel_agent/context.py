"""Working memory carried across the multi-agent pipeline for one chapter.

The pipeline hands one :class:`AgentContext` from stage to stage (outline ->
chapter -> polish). Each completed stage stores its structured output in
``stage_results``; downstream stages read the previous stage's result, and the
whole map is serialised into a checkpoint at every stage boundary so a crashed
run can resume without re-calling the model for work it already finished.
"""
import json


class AgentContext:
    def __init__(self, novel, recent, bible, skill, chapter_number, target_words=0):
        self.novel = novel
        self.recent = recent
        self.bible = bible            # bounded digest used in prompts
        self.full_bible = novel.get("story_bible", {})  # authoritative, untruncated
        self.skill = skill
        self.chapter_number = chapter_number
        self.target_words = target_words
        self.stage_results = {}       # stage name -> structured output (memory)

    def set_result(self, stage, output):
        self.stage_results[stage] = output

    def get_result(self, stage):
        return self.stage_results.get(stage)

    def memory_json(self):
        """Serialise the accumulated stage results for checkpointing."""
        return json.dumps(self.stage_results, ensure_ascii=False)

    def load_memory(self, memory):
        """Restore ``stage_results`` from a checkpoint snapshot (idempotent)."""
        if isinstance(memory, dict):
            self.stage_results = memory
        elif memory:
            try:
                parsed = json.loads(memory)
                if isinstance(parsed, dict):
                    self.stage_results = parsed
            except (TypeError, ValueError):
                self.stage_results = {}
        return self
