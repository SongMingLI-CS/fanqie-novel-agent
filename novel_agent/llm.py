"""Asyncio DeepSeek client with tenacity-managed exponential backoff.

The async client mirrors the sync client's error taxonomy (see ``deepseek.py``)
so the console and ``usage`` records behave identically. The differences:

* the whole call path is ``asyncio`` over ``httpx``;
* tenacity owns the retry policy (``stop_after_attempt`` from
  ``NOVEL_MAX_RETRIES``) with a custom wait that honours a 429 ``Retry-After``
  header and otherwise doubles from 2s up to a 30s cap;
* ``stream()`` is an async generator yielding ``delta`` chunks as they arrive,
  followed by a final ``usage`` chunk — the consumer can persist progress or
  forward it to the browser while the response is still being generated.

Only pre-body failures (connection/TLS/DNS/HTTP status) are auto-retried; once
the body has started, a read failure is surfaced as a ``DeepSeekError`` and the
caller decides (job-level resume/retry) instead of silently duplicating tokens.
"""
import json
import logging
import os
import socket
import ssl
import time

import httpx
from tenacity import (
    before_sleep_log,
    retry,
    retry_if_exception,
    stop_after_attempt,
)

from .deepseek import DeepSeekError
from .prompts import VERSION as PROMPT_VERSION

logger = logging.getLogger(__name__)

# Categories that must never be retried: configuration and contract errors are
# deterministic; mid-stream interruptions must not silently re-run a call that
# already emitted tokens.
_NON_RETRYABLE = frozenset({
    "configuration", "http_400", "http_401", "http_404",
    "stream_interrupted", "stream_parse_error", "output_truncated",
})

_HTTP_CATEGORY = {400: "http_400", 401: "http_401", 404: "http_404", 429: "http_429"}


class AsyncLLMClient:
    """OpenAI-compatible async client with retry/backoff over httpx."""

    def __init__(self, config, transport=None):
        self.config = config
        kwargs = {}
        if config.ca_bundle:
            kwargs["verify"] = config.ca_bundle
        timeout = httpx.Timeout(config.timeout, connect=config.connect_timeout)
        self._client = httpx.AsyncClient(timeout=timeout, transport=transport, **kwargs)

    async def aclose(self):
        await self._client.aclose()

    # -- request preparation -------------------------------------------------

    def _payload(self, system, user):
        api_key = os.getenv("DEEPSEEK_API_KEY", "")
        if not api_key:
            raise DeepSeekError("DEEPSEEK_API_KEY is not configured", "configuration")
        if api_key != api_key.strip():
            raise DeepSeekError("DEEPSEEK_API_KEY has surrounding whitespace", "configuration")
        if not self.config.base_url:
            raise DeepSeekError("DEEPSEEK_BASE_URL is not configured")
        if not self.config.model:
            raise DeepSeekError("DEEPSEEK_MODEL is not configured")
        if self.config.thinking not in ("enabled", "disabled"):
            raise DeepSeekError("DEEPSEEK_THINKING must be enabled or disabled", "configuration")
        url = httpx.URL(self.config.base_url)
        if url.scheme != "https":
            raise DeepSeekError("DEEPSEEK_BASE_URL must be HTTPS", "configuration")
        payload = {
            "model": self.config.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": self.config.max_tokens,
            "stream": True,
            "stream_options": {"include_usage": True},
            "response_format": {"type": "json_object"},
            "thinking": {"type": self.config.thinking},
        }
        if self.config.thinking == "enabled":
            payload["reasoning_effort"] = self.config.reasoning_effort
        headers = {"Authorization": "Bearer " + api_key, "Content-Type": "application/json"}
        return payload, headers
    # -- error classification ------------------------------------------------

    @staticmethod
    def _http_error(status, headers=None):
        retry_after = None
        if status == 429 and headers:
            try:
                retry_after = float(headers.get("Retry-After", ""))
            except (TypeError, ValueError):
                pass
            if retry_after is not None and retry_after != retry_after:  # NaN guard
                retry_after = None
        category = _HTTP_CATEGORY.get(status, "http_error")
        return DeepSeekError(f"DeepSeek HTTP {status}", category, status, retry_after)

    @classmethod
    def _classify(cls, exc):
        cause = exc
        while cause is not None:
            if isinstance(cause, ssl.SSLError):
                return DeepSeekError("DeepSeek TLS failure", "tls_error")
            if isinstance(cause, socket.gaierror):
                return DeepSeekError("DeepSeek DNS failure", "dns_error")
            cause = getattr(cause, "__cause__", None)
        if isinstance(exc, httpx.ConnectTimeout):
            return DeepSeekError("DeepSeek connection timeout", "connect_timeout")
        if isinstance(exc, (httpx.ConnectError, httpx.ProxyError)):
            return DeepSeekError("DeepSeek TCP connection failure", "tcp_connection_error")
        if isinstance(exc, httpx.TimeoutException):
            return DeepSeekError("DeepSeek request timeout", "stream_read_timeout")
        return DeepSeekError(f"DeepSeek response failure: {type(exc).__name__}", "response_error")

    # -- retry policy --------------------------------------------------------

    @classmethod
    def _retryable(cls, exc):
        return isinstance(exc, DeepSeekError) and exc.category not in _NON_RETRYABLE

    @staticmethod
    def _wait(retry_state):
        exc = retry_state.outcome.exception()
        if isinstance(exc, DeepSeekError) and exc.retry_after is not None:
            return max(0.0, min(float(exc.retry_after), 30.0))
        attempts = max(0, int(getattr(retry_state, "attempt_number", 1)) - 1)
        return min(2.0 ** attempts, 30.0)

    # -- low level -----------------------------------------------------------

    async def _attempt_open(self, payload, headers):
        url = str(httpx.URL(self.config.base_url).join("chat/completions"))
        try:
            resp = await self._client.send(
                self._client.build_request("POST", url, json=payload, headers=headers),
                stream=True,
            )
        except httpx.TimeoutException as exc:
            raise self._classify(exc) from exc
        except httpx.HTTPError as exc:
            raise self._classify(exc) from exc
        if resp.status_code >= 400:
            try:
                await resp.aread()
            finally:
                await resp.aclose()
            raise self._http_error(resp.status_code, resp.headers)
        return resp

    async def _open(self, payload, headers):
        """Open the streamed response, retrying pre-body failures via tenacity."""
        wrapped = retry(
            stop=stop_after_attempt(self.config.max_retries + 1),
            wait=self._wait,
            retry=retry_if_exception(self._retryable),
            reraise=True,
            before_sleep=before_sleep_log(logger, logging.WARNING),
        )(self._attempt_open)
        return await wrapped(payload, headers)
    # -- public API ----------------------------------------------------------

    async def stream(self, system, user):
        """Yield ``{'type': 'delta', 'text': ...}`` then a ``usage`` chunk.

        Raises :class:`DeepSeekError` with the same taxonomy as the sync client.
        """
        payload, headers = self._payload(system, user)
        started = time.monotonic()
        deadline = started + self.config.timeout
        resp = await self._open(payload, headers)
        content = []
        usage = {}
        finish_reason = None
        saw_done = False
        emitted = False
        try:
            try:
                async for line in resp.aiter_lines():
                    if time.monotonic() > deadline:
                        raise DeepSeekError("DeepSeek overall request timeout", "overall_timeout")
                    if not line.startswith("data:"):
                        continue
                    raw = line[5:].strip()
                    if not raw:
                        continue
                    if raw == "[DONE]":
                        saw_done = True
                        break
                    try:
                        item = json.loads(raw)
                    except json.JSONDecodeError:
                        raise DeepSeekError("DeepSeek malformed stream event", "stream_parse_error")
                    choice = (item.get("choices") or [{}])[0]
                    delta = choice.get("delta", {}) or {}
                    piece = delta.get("content")
                    if isinstance(piece, str) and piece:
                        content.append(piece)
                        emitted = True
                        yield {"type": "delta", "text": piece}
                    if choice.get("finish_reason") is not None:
                        finish_reason = choice["finish_reason"]
                    if item.get("usage"):
                        usage = item["usage"]
            except httpx.TimeoutException as exc:
                category = "stream_read_timeout" if emitted else "first_byte_timeout"
                raise DeepSeekError("DeepSeek stream timeout", category) from exc
            except httpx.HTTPError as exc:
                raise DeepSeekError("DeepSeek stream interrupted", "stream_interrupted") from exc
        finally:
            await resp.aclose()
        if not saw_done:
            raise DeepSeekError("DeepSeek stream interrupted before DONE", "stream_interrupted")
        if finish_reason == "length":
            raise DeepSeekError("DeepSeek output truncated at max_tokens", "output_truncated")
        elapsed_ms = round((time.monotonic() - started) * 1000)
        yield {
            "type": "usage",
            "usage": {
                "model": self.config.model,
                "prompt_version": PROMPT_VERSION,
                "input_tokens": usage.get("prompt_tokens", 0),
                "output_tokens": usage.get("completion_tokens", 0),
                "duration_ms": elapsed_ms,
                "request_status": "succeeded",
            },
        }

    async def complete(self, system, user):
        """Accumulate a full streaming response into ``(message, usage)``."""
        parts = []
        usage = {}
        async for chunk in self.stream(system, user):
            if chunk["type"] == "delta":
                parts.append(chunk["text"])
            elif chunk["type"] == "usage":
                usage = chunk["usage"]
        message = "".join(parts)
        if not message.strip():
            raise DeepSeekError("DeepSeek returned empty message.content", "empty_content")
        return message, usage
