"""Model adapters.

Only three things are required of a model: complete a tagged prompt, report
its cost, and be constructible twice so the verifier never shares an instance
with the executor.  Everything else (routing, scripted fixtures, HTTP) is a
thin wrapper, which is what keeps the runtime testable offline.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Protocol, runtime_checkable

#: every prompt starts with a machine-readable tag such as ``task=plan``
TAG_RE = re.compile(r"^\s*task=([a-z_]+)\s*$", re.MULTILINE)


@dataclass
class Completion:
    text: str
    tokens_in: int = 0
    tokens_out: int = 0
    cached_tokens: int = 0

    @property
    def tokens(self) -> int:
        return self.tokens_in + self.tokens_out


@runtime_checkable
class Model(Protocol):
    name: str

    def complete(self, prompt: str, *, max_tokens: int = 900) -> Completion: ...


def estimate_tokens(text: str) -> int:
    """Cheap, deliberately conservative token estimate (no tokeniser needed)."""
    return max(1, int(len(text) / 3.2))


def task_tag(prompt: str) -> str:
    m = TAG_RE.search(prompt)
    return m.group(1) if m else "unknown"


def extract_json(text: str) -> dict[str, Any]:
    """Pull the first JSON object out of a model reply.

    Models like to wrap JSON in prose or fences; we accept that, but we do not
    repair malformed JSON -- a malformed plan is a failure signal, not a
    prompt to be extra clever.
    """
    if isinstance(text, dict):
        return text
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text).strip()
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass
    depth = 0
    start = -1
    in_str = False
    esc = False
    for i, ch in enumerate(text):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start >= 0:
                try:
                    obj = json.loads(text[start : i + 1])
                except json.JSONDecodeError:
                    start = -1
                    continue
                if isinstance(obj, dict):
                    return obj
    raise ValueError(f"no JSON object in model reply: {text[:200]!r}")


class ScriptedModel:
    """Deterministic model driven by a handler, for demos, tests and replays."""

    def __init__(self, handler: Callable[[str], str], *, name: str = "scripted"):
        self.handler = handler
        self.name = name
        self.calls = 0
        self.tokens_used = 0

    def complete(self, prompt: str, *, max_tokens: int = 900) -> Completion:
        self.calls += 1
        text = self.handler(prompt)
        comp = Completion(
            text=text,
            tokens_in=estimate_tokens(prompt),
            tokens_out=estimate_tokens(text),
        )
        self.tokens_used += comp.tokens
        return comp


class RoutingModel:
    """Route by prompt tag: cheap models for judging/summarising, strong for planning."""

    def __init__(self, routes: Mapping[str, Model], default: Model | None = None):
        self.routes = dict(routes)
        self.default = default
        self.name = "routing(" + ",".join(sorted(self.routes)) + ")"

    def for_prompt(self, prompt: str) -> Model:
        tag = task_tag(prompt)
        if tag in self.routes:
            return self.routes[tag]
        if self.default is not None:
            return self.default
        raise KeyError(f"no model route for task={tag}")

    def complete(self, prompt: str, *, max_tokens: int = 900) -> Completion:
        return self.for_prompt(prompt).complete(prompt, max_tokens=max_tokens)


class OpenAICompatModel:
    """Minimal chat-completions client (OpenAI-compatible endpoints, stdlib only).

    Configure with ``PLANLOOP_BASE_URL``, ``PLANLOOP_API_KEY``, ``PLANLOOP_MODEL``.
    """

    def __init__(
        self,
        model: str,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        temperature: float = 0.2,
        timeout: float = 120.0,
    ):
        self.name = f"openai-compat:{model}"
        self.model = model
        self.base_url = (base_url or os.environ.get("PLANLOOP_BASE_URL", "https://api.openai.com/v1")).rstrip("/")
        self.api_key = api_key or os.environ.get("PLANLOOP_API_KEY", "")
        self.temperature = temperature
        self.timeout = timeout

    def complete(self, prompt: str, *, max_tokens: int = 900) -> Completion:
        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": self.temperature,
            "max_tokens": max_tokens,
        }
        req = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:  # pragma: no cover - network path
            raise RuntimeError(f"model HTTP {exc.code}: {exc.read()[:400]!r}") from exc
        usage = data.get("usage") or {}
        cached = int((usage.get("prompt_tokens_details") or {}).get("cached_tokens") or 0)
        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        # reasoning models may return an empty `content` when they hit the token
        # cap while thinking; fall back to the reasoning channel, then to a
        # readable error rather than a KeyError deep inside the harness.
        text = message.get("content") or message.get("reasoning_content") or ""
        if not text:
            finish = choice.get("finish_reason", "?")
            raise RuntimeError(
                f"model returned no content (finish_reason={finish}); "
                "raise max_tokens or lower the prompt size"
            )
        return Completion(
            text=text,
            cached_tokens=cached,
            tokens_in=int(usage.get("prompt_tokens", estimate_tokens(prompt))),
            tokens_out=int(usage.get("completion_tokens", estimate_tokens(text))),
        )


def make_model_from_env() -> Model | None:
    """Build a real model if credentials exist, otherwise signal offline mode."""
    model = os.environ.get("PLANLOOP_MODEL")
    if not model:
        return None
    return OpenAICompatModel(model)


class CountingModel:
    """Transparent wrapper that records spend per run (used by the bench)."""

    def __init__(self, inner: Model):
        self.inner = inner
        self.name = f"counting({inner.name})"
        self.calls = 0
        self.tokens = 0

    def complete(self, prompt: str, *, max_tokens: int = 900) -> Completion:
        comp = self.inner.complete(prompt, max_tokens=max_tokens)
        self.calls += 1
        self.tokens += comp.tokens
        return comp
