"""Tests for AsyncLLMClient (httpx + tenacity) over a mock transport."""
import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path

import httpx

from novel_agent.config import Config
from novel_agent.deepseek import DeepSeekError
from novel_agent.llm import AsyncLLMClient


def sse_body(chunks, finish="stop", with_usage=True, done=True):
    lines = []
    for chunk in chunks:
        lines.append(
            "data: " + json.dumps({"choices": [{"delta": {"content": chunk}, "index": 0}]})
        )
    lines.append(
        "data: " + json.dumps({"choices": [{"delta": {}, "finish_reason": finish, "index": 0}]})
    )
    if with_usage:
        lines.append("data: " + json.dumps({"usage": {"prompt_tokens": 11, "completion_tokens": 22}}))
    if done:
        lines.append("data: [DONE]")
    return ("\n".join(lines) + "\n\n").encode("utf-8")


class AsyncClientTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._key = os.environ.get("DEEPSEEK_API_KEY")
        os.environ["DEEPSEEK_API_KEY"] = "sk-test-0123456789"

    def tearDown(self):
        if self._key is None:
            os.environ.pop("DEEPSEEK_API_KEY", None)
        else:
            os.environ["DEEPSEEK_API_KEY"] = self._key
        self.tmp.cleanup()

    def _client(self, handler, max_retries=2):
        config = Config(
            data_dir=Path(self.tmp.name),
            base_url="https://api.deepseek.com",
            model="deepseek-v4",
            thinking="disabled",
            max_retries=max_retries,
            timeout=60,
            connect_timeout=5,
            max_tokens=100,
        )
        return AsyncLLMClient(config, transport=httpx.MockTransport(handler))

    def _run(self, coro):
        return asyncio.run(coro)

    def test_complete_accumulates_message_and_usage(self):
        calls = []

        def handler(request):
            calls.append(request.url)
            return httpx.Response(200, content=sse_body(["一", "二"]),
                                  headers={"content-type": "text/event-stream"})

        client = self._client(handler)
        message, usage = self._run(client.complete("sys", "user"))
        self.assertEqual(message, "一二")
        self.assertEqual(usage["input_tokens"], 11)
        self.assertEqual(usage["output_tokens"], 22)
        self.assertEqual(usage["request_status"], "succeeded")
        self.assertEqual(len(calls), 1)

    def test_stream_yields_deltas_then_usage(self):
        def handler(request):
            return httpx.Response(200, content=sse_body(["甲", "乙", "丙"]),
                                  headers={"content-type": "text/event-stream"})

        client = self._client(handler)
        deltas, kinds = [], []
        async def collect():
            async for chunk in client.stream("sys", "user"):
                kinds.append(chunk["type"])
                if chunk["type"] == "delta":
                    deltas.append(chunk["text"])
        self._run(collect())
        self.assertEqual("".join(deltas), "甲乙丙")
        self.assertEqual(kinds, ["delta", "delta", "delta", "usage"])

    def test_429_is_retried_with_retry_after_then_succeeds(self):
        calls = []

        def handler(request):
            calls.append(request.url)
            if len(calls) == 1:
                return httpx.Response(429, content=b"", headers={"Retry-After": "0"})
            return httpx.Response(200, content=sse_body(["重试成功"]),
                                  headers={"content-type": "text/event-stream"})

        client = self._client(handler)
        message, _ = self._run(client.complete("sys", "user"))
        self.assertEqual(message, "重试成功")
        self.assertEqual(len(calls), 2)

    def test_429_exhaustion_raises_after_max_attempts(self):
        calls = []

        def handler(request):
            calls.append(request.url)
            return httpx.Response(429, content=b"", headers={"Retry-After": "0"})

        client = self._client(handler, max_retries=2)
        with self.assertRaises(DeepSeekError) as ctx:
            self._run(client.complete("sys", "user"))
        self.assertEqual(ctx.exception.category, "http_429")
        self.assertEqual(len(calls), 3)  # max_retries + initial attempt

    def test_http_400_is_never_retried(self):
        calls = []

        def handler(request):
            calls.append(request.url)
            return httpx.Response(400, content=b"bad")

        client = self._client(handler, max_retries=3)
        with self.assertRaises(DeepSeekError) as ctx:
            self._run(client.complete("sys", "user"))
        self.assertEqual(ctx.exception.category, "http_400")
        self.assertEqual(len(calls), 1)

    def test_missing_done_marks_stream_interrupted(self):
        def handler(request):
            return httpx.Response(200, content=sse_body(["残"], done=False),
                                  headers={"content-type": "text/event-stream"})

        client = self._client(handler)
        with self.assertRaises(DeepSeekError) as ctx:
            self._run(client.complete("sys", "user"))
        self.assertEqual(ctx.exception.category, "stream_interrupted")

    def test_truncated_output_raises_output_truncated(self):
        def handler(request):
            return httpx.Response(200, content=sse_body(["超长"], finish="length"),
                                  headers={"content-type": "text/event-stream"})

        client = self._client(handler)
        with self.assertRaises(DeepSeekError) as ctx:
            self._run(client.complete("sys", "user"))
        self.assertEqual(ctx.exception.category, "output_truncated")


if __name__ == "__main__":
    unittest.main()
