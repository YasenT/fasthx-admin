"""
AI Chat framework for fasthx-admin.

Provides a pluggable AI chat widget with:
- Provider abstraction (ships with OpenAI-compatible)
- Decorator-based tool registry
- Settings stored in DB
- Chat endpoints as a FastAPI router
"""

from __future__ import annotations

import asyncio
import collections
import datetime
import inspect as python_inspect
import json
import logging
import re
import time
import uuid
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, get_type_hints

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from sqlalchemy import Boolean, Column, DateTime, Float, Integer, String, Text, inspect, text
from sqlalchemy.orm import Session

from .auth import get_current_user
from .database import Base, get_db, get_engine

logger = logging.getLogger("fasthx_admin.ai_chat")

_PACKAGE_DIR = Path(__file__).resolve().parent


# ---------------------------------------------------------------------------
# Diagnostics (Postgres-backed)
# ---------------------------------------------------------------------------
# Earlier versions used an in-memory deque. That breaks multi-replica
# deployments: a chat request lands on pod A (logs to its deque) while the
# diagnostics page loads from pod B (sees empty). Persisting to the existing
# DB makes the log visible across replicas and survive restarts.
#
# `_DIAGNOSTICS_FALLBACK` is only used if the DB write fails — never let a
# logging error break a chat request.

_DIAGNOSTICS_MAX_ROWS = 200
_DIAGNOSTICS_FALLBACK: "collections.deque[dict]" = collections.deque(maxlen=_DIAGNOSTICS_MAX_ROWS)


def _log_diagnostic(event_type: str, detail: str, **extra: Any) -> None:
    ts = datetime.datetime.now(datetime.timezone.utc)
    entry = {
        "ts": ts.isoformat(timespec="seconds"),
        "type": event_type,
        "detail": detail,
    }
    if extra:
        entry["extra"] = extra
    logger.info("ai_chat diagnostic: %s — %s", event_type, detail)

    # Best-effort DB write. Never raise from a logging path.
    try:
        db = next(get_db())
        try:
            row = AIChatDiagnostic(
                ts=ts,
                event_type=event_type[:64],
                detail=detail[:1000],
                extra=json.dumps(extra) if extra else None,
            )
            db.add(row)
            db.commit()
            # Trim. We do this opportunistically rather than on every insert
            # to avoid an N+1 — once every ~50 writes is enough to keep the
            # table bounded to roughly _DIAGNOSTICS_MAX_ROWS.
            if (row.id or 0) % 50 == 0:
                _trim_diagnostics(db)
        finally:
            db.close()
        return
    except Exception:
        logger.exception("ai_chat diagnostic DB write failed; falling back to memory")
        _DIAGNOSTICS_FALLBACK.append(entry)


def _trim_diagnostics(db: Session) -> None:
    """Delete rows older than the most recent _DIAGNOSTICS_MAX_ROWS."""
    try:
        cutoff = (
            db.query(AIChatDiagnostic.id)
            .order_by(AIChatDiagnostic.id.desc())
            .offset(_DIAGNOSTICS_MAX_ROWS)
            .first()
        )
        if cutoff:
            db.query(AIChatDiagnostic).filter(
                AIChatDiagnostic.id <= cutoff.id
            ).delete(synchronize_session=False)
            db.commit()
    except Exception:
        db.rollback()
        logger.exception("ai_chat diagnostic trim failed")


def get_diagnostics(db: Session | None = None) -> list[dict]:
    """Return diagnostics entries, newest first."""
    own_session = db is None
    if own_session:
        db = next(get_db())
    try:
        rows = (
            db.query(AIChatDiagnostic)
            .order_by(AIChatDiagnostic.id.desc())
            .limit(_DIAGNOSTICS_MAX_ROWS)
            .all()
        )
        out: list[dict] = []
        for r in rows:
            entry: dict[str, Any] = {
                "ts": r.ts.isoformat(timespec="seconds") if r.ts else "",
                "type": r.event_type,
                "detail": r.detail,
            }
            if r.extra:
                try:
                    entry["extra"] = json.loads(r.extra)
                except (json.JSONDecodeError, TypeError):
                    entry["extra"] = {"raw": r.extra}
            out.append(entry)
        # Append in-memory fallback (only present if DB writes have failed)
        if _DIAGNOSTICS_FALLBACK:
            out.extend(reversed(_DIAGNOSTICS_FALLBACK))
        return out
    except Exception:
        logger.exception("ai_chat diagnostic read failed; using fallback")
        return list(reversed(_DIAGNOSTICS_FALLBACK))
    finally:
        if own_session:
            db.close()


def clear_diagnostics(db: Session) -> None:
    db.query(AIChatDiagnostic).delete(synchronize_session=False)
    db.commit()
    _DIAGNOSTICS_FALLBACK.clear()


# ---------------------------------------------------------------------------
# Gemma tool-call fallback parser
# ---------------------------------------------------------------------------
# When vLLM's tool-call parser can't recognize the model output, it returns the
# raw template tokens as content. We regex out a best-effort tool call so the
# user gets a working answer instead of "<|tool_call>..." gibberish.

_GEMMA_TOOL_CALL_RE = re.compile(r"<\|tool_call>(.+?)<tool_call\|>", re.DOTALL)
# Inside the wrapper we accept three observed shapes:
#   call:fn{key:<|"|>val<|"|>,key:<|"|>val<|"|>}     (the malformed one)
#   fn(key="val", key="val")                          (canonical pythonic)
#   {"name":"fn","arguments":{...}}                   (Hermes-style JSON)
_GEMMA_PYTHONIC_RE = re.compile(
    r"(?:call:)?\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*[\{\(](.*)[\}\)]\s*$",
    re.DOTALL,
)


def _normalize_gemma_quotes(s: str) -> str:
    """Convert Gemma's <|"|> quote tokens back to standard quotes."""
    return s.replace("<|\"|>", '"').replace("<|'|>", "'")


def _parse_gemma_tool_call(content: str) -> dict | None:
    """Best-effort parse of a malformed Gemma tool call from raw content.

    Returns an OpenAI-shaped tool_call dict, or None if nothing recoverable.
    """
    if not content or "<|tool_call" not in content:
        return None

    match = _GEMMA_TOOL_CALL_RE.search(content)
    if not match:
        return None

    body = _normalize_gemma_quotes(match.group(1).strip())

    # Try Hermes-style JSON first.
    if body.startswith("{") and "\"name\"" in body:
        try:
            obj = json.loads(body)
            return {
                "id": f"call_{uuid.uuid4().hex[:12]}",
                "type": "function",
                "function": {
                    "name": obj["name"],
                    "arguments": json.dumps(obj.get("arguments") or {}),
                },
            }
        except (json.JSONDecodeError, KeyError):
            pass

    m = _GEMMA_PYTHONIC_RE.match(body)
    if not m:
        return None
    fn_name, args_blob = m.group(1), m.group(2).strip()

    args: dict[str, Any] = {}
    # Split on commas not inside quotes — naive but sufficient for the
    # observed shapes (no nested objects in real tool args).
    parts = re.findall(r'([a-zA-Z_][a-zA-Z0-9_]*)\s*[:=]\s*("(?:[^"\\]|\\.)*"|\'(?:[^\'\\]|\\.)*\'|[^,]+)', args_blob)
    for key, raw in parts:
        raw = raw.strip().rstrip(",").strip()
        if (raw.startswith('"') and raw.endswith('"')) or (raw.startswith("'") and raw.endswith("'")):
            args[key] = raw[1:-1]
        else:
            try:
                args[key] = json.loads(raw)
            except json.JSONDecodeError:
                args[key] = raw

    return {
        "id": f"call_{uuid.uuid4().hex[:12]}",
        "type": "function",
        "function": {
            "name": fn_name,
            "arguments": json.dumps(args),
        },
    }


# ---------------------------------------------------------------------------
# Thinking extraction
# ---------------------------------------------------------------------------
# Two paths: vLLM's reasoning parser puts the chain-of-thought in
# `reasoning_content`; without it, the thoughts arrive inline in `content`
# wrapped in <think>...</think> or <|think|>...<|/think|> tags.

_THINK_RE = re.compile(
    r"(?:<\|think\|>|<think>)(.*?)(?:<\|/think\|>|</think>)",
    re.DOTALL,
)


def _extract_thinking(message: dict) -> tuple[str, str]:
    """Return (thinking_text, cleaned_content). Either may be empty."""
    reasoning = message.get("reasoning_content") or ""
    content = message.get("content") or ""

    if reasoning:
        return reasoning.strip(), content

    matches = _THINK_RE.findall(content)
    if not matches:
        return "", content

    thoughts = "\n\n".join(t.strip() for t in matches if t.strip())
    cleaned = _THINK_RE.sub("", content).strip()
    return thoughts, cleaned


# ---------------------------------------------------------------------------
# Keycloak (OAuth2 client_credentials) token manager
# ---------------------------------------------------------------------------


class KeycloakTokenManager:
    """Fetches and caches OAuth2 client_credentials tokens from a Keycloak realm.

    Instances are deduplicated by (auth_url, client_id, client_secret) so multiple
    providers configured against the same client share a token + cache.
    """

    _instances: dict[tuple, "KeycloakTokenManager"] = {}

    def __new__(
        cls,
        auth_url: str,
        client_id: str,
        client_secret: str,
        ssl_verify: bool = False,
    ):
        key = (auth_url, client_id, client_secret)
        if key not in cls._instances:
            instance = super().__new__(cls)
            instance._initialized = False
            cls._instances[key] = instance
        return cls._instances[key]

    def __init__(
        self,
        auth_url: str,
        client_id: str,
        client_secret: str,
        ssl_verify: bool = False,
    ):
        if self._initialized:
            # ssl_verify may have been toggled in the connection — keep it fresh.
            self.ssl_verify = ssl_verify
            return
        self.auth_url = auth_url
        self.client_id = client_id
        self.client_secret = client_secret
        self.ssl_verify = ssl_verify
        self.token: str | None = None
        self.expires_at: float = 0.0
        self._initialized = True

    def get_token(self) -> str | None:
        # Refresh if missing or expiring within 60 seconds.
        if not self.token or time.time() > (self.expires_at - 60):
            self._refresh_token()
        return self.token

    def _refresh_token(self) -> None:
        import requests
        try:
            import urllib3
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        except Exception:
            pass

        logger.info("Fetching new OAuth token from Keycloak (%s)", self.auth_url)
        payload = {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "grant_type": "client_credentials",
        }
        response = requests.post(
            self.auth_url, data=payload, verify=self.ssl_verify, timeout=10
        )
        response.raise_for_status()
        token_data = response.json()
        self.token = token_data.get("access_token")
        expires_in = token_data.get("expires_in", 300)
        self.expires_at = time.time() + expires_in


# ---------------------------------------------------------------------------
# Provider Abstraction
# ---------------------------------------------------------------------------


class AIProvider(ABC):
    """Base class for AI providers."""

    name: str = "base"

    @abstractmethod
    async def chat(
        self, messages: list[dict], tools: list[dict] | None = None, **kwargs
    ) -> dict:
        """Send messages to the AI and return the response.

        Returns dict with keys: response (str), tool_calls (list | None)
        """
        ...

    @abstractmethod
    def get_config_fields(self) -> list[dict]:
        """Return provider-specific settings fields for the settings UI."""
        ...


class OpenAICompatibleProvider(AIProvider):
    """Works with OpenAI, vLLM, Ollama, LiteLLM, etc."""

    name = "openai_compatible"

    def __init__(
        self,
        base_url: str = "https://api.openai.com",
        api_key: str = "",
        model: str = "gpt-4o-mini",
        temperature: float = 0.7,
        max_tokens: int = 2048,
        timeout: float = 60.0,
        ssl_verify: bool = True,
        token_manager: KeycloakTokenManager | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.timeout = timeout
        self.ssl_verify = ssl_verify
        self.token_manager = token_manager

    async def chat(
        self, messages: list[dict], tools: list[dict] | None = None, **kwargs
    ) -> dict:
        try:
            import httpx
        except ImportError:
            raise RuntimeError(
                "httpx is required for AI chat. Install with: pip install fasthx-admin[ai]"
            )

        url = f"{self.base_url}/v1/chat/completions"
        headers = {"Content-Type": "application/json"}
        if self.token_manager is not None:
            # Off-thread to avoid blocking the event loop on the (rare) refresh.
            token = await asyncio.to_thread(self.token_manager.get_token)
            if token:
                headers["Authorization"] = f"Bearer {token}"
        elif self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }
        if tools:
            payload["tools"] = tools
        if kwargs.get("thinking"):
            # vLLM forwards chat_template_kwargs into apply_chat_template().
            # Gemma uses enable_thinking; preserve any caller-supplied kwargs.
            existing = dict(payload.get("chat_template_kwargs") or {})
            existing["enable_thinking"] = True
            payload["chat_template_kwargs"] = existing

        async with httpx.AsyncClient(timeout=self.timeout, verify=self.ssl_verify) as client:
            resp = await client.post(url, json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()

        choice = data["choices"][0]
        message = choice["message"]
        tool_calls = message.get("tool_calls")
        content = message.get("content") or ""

        # Fallback: vLLM couldn't parse the tool call but the raw markup is
        # still in `content`. Try to recover so the tool actually fires.
        if not tool_calls and "<|tool_call" in content:
            recovered = _parse_gemma_tool_call(content)
            if recovered:
                tool_calls = [recovered]
                _log_diagnostic(
                    "tool_call_fallback_parsed",
                    f"Recovered malformed tool call: {recovered['function']['name']}",
                    model=self.model,
                )
                content = ""
            else:
                _log_diagnostic(
                    "tool_call_unparseable",
                    "Tool-call markup in content but fallback could not extract a call",
                    model=self.model,
                    snippet=content[:200],
                )

        thinking, cleaned = _extract_thinking({**message, "content": content})

        # When the user has thinking mode on, record what vLLM actually
        # returned. This is the only way to tell whether the server-side
        # reasoning parser is configured, whether the model emitted any
        # thinking tokens at all, and what tag format it used.
        if kwargs.get("thinking"):
            raw_reasoning = message.get("reasoning_content") or ""
            raw_content = message.get("content") or ""
            _log_diagnostic(
                "thinking_response",
                f"thinking_len={len(thinking)} content_len={len(cleaned)}",
                model=self.model,
                had_reasoning_content=bool(raw_reasoning),
                had_think_tags=bool(_THINK_RE.search(raw_content)),
                content_snippet=raw_content[:500],
                reasoning_snippet=raw_reasoning[:500],
            )

        return {
            "response": cleaned,
            "tool_calls": tool_calls,
            "thinking": thinking,
        }

    def get_config_fields(self) -> list[dict]:
        return [
            {"key": "base_url", "label": "API Base URL", "type": "text", "default": "https://api.openai.com"},
            {"key": "api_key", "label": "API Key", "type": "password", "default": ""},
            {"key": "model", "label": "Model", "type": "text", "default": "gpt-4o-mini"},
            {"key": "temperature", "label": "Temperature", "type": "number", "default": "0.7", "step": "0.1", "min": "0", "max": "2"},
            {"key": "max_tokens", "label": "Max Tokens", "type": "number", "default": "2048"},
            {"key": "timeout", "label": "Timeout (seconds)", "type": "number", "default": "60"},
            {"key": "ssl_verify", "label": "Verify SSL", "type": "checkbox", "default": "true"},
        ]


# ---------------------------------------------------------------------------
# Tool Registry
# ---------------------------------------------------------------------------


class ToolDef:
    """Metadata for a registered tool."""

    def __init__(self, func: Callable, name: str, description: str, parameters: dict):
        self.func = func
        self.name = name
        self.description = description
        self.parameters = parameters


HOOK_EVENTS = ("user_prompt_submit", "pre_tool_use", "post_tool_use", "session_start")


class HookDef:
    """Metadata for a registered lifecycle hook."""

    def __init__(self, func: Callable, name: str, event: str, description: str):
        self.func = func
        self.name = name
        self.event = event
        self.description = description


class ToolRegistry:
    """Decorator-based tool and lifecycle-hook registration."""

    def __init__(self):
        self._tools: dict[str, ToolDef] = {}
        self._hooks: dict[str, list[HookDef]] = {}

    def tool(self, name: str | None = None, description: str | None = None):
        """Decorator to register a function as an AI tool."""

        def decorator(func: Callable) -> Callable:
            tool_name = name or func.__name__
            tool_desc = description or (func.__doc__ or "").strip() or tool_name
            params = self._extract_parameters(func)
            self._tools[tool_name] = ToolDef(func, tool_name, tool_desc, params)
            return func

        return decorator

    def _extract_parameters(self, func: Callable) -> dict:
        """Extract parameter schema from type hints."""
        sig = python_inspect.signature(func)
        hints = get_type_hints(func) if hasattr(func, "__annotations__") else {}
        properties = {}
        required = []

        type_map = {
            str: "string",
            int: "integer",
            float: "number",
            bool: "boolean",
        }

        for param_name, param in sig.parameters.items():
            if param_name == "db":
                continue
            hint = hints.get(param_name)
            if hint is None:
                continue
            json_type = type_map.get(hint, "string")
            properties[param_name] = {"type": json_type, "description": param_name}
            if param.default is python_inspect.Parameter.empty:
                required.append(param_name)

        schema: dict[str, Any] = {"type": "object", "properties": properties}
        if required:
            schema["required"] = required
        return schema

    def get_openai_tools(self, enabled_tools: set[str] | None = None) -> list[dict]:
        """Return tools in OpenAI function-calling format."""
        tools = []
        for tool_def in self._tools.values():
            if enabled_tools is not None and tool_def.name not in enabled_tools:
                continue
            tools.append({
                "type": "function",
                "function": {
                    "name": tool_def.name,
                    "description": tool_def.description,
                    "parameters": tool_def.parameters,
                },
            })
        return tools

    async def execute(self, name: str, arguments: dict, db: Session | None = None) -> str:
        """Execute a registered tool by name."""
        tool_def = self._tools.get(name)
        if not tool_def:
            return f"Error: Unknown tool '{name}'"
        try:
            sig = python_inspect.signature(tool_def.func)
            kwargs = dict(arguments)
            if "db" in sig.parameters and db is not None:
                kwargs["db"] = db
            if python_inspect.iscoroutinefunction(tool_def.func):
                result = await tool_def.func(**kwargs)
            else:
                result = tool_def.func(**kwargs)
            return str(result)
        except Exception as e:
            logger.exception("Tool execution failed: %s", name)
            return f"Error executing tool '{name}': {e}"

    def list_tools(self) -> list[dict]:
        """List all registered tools with metadata."""
        return [
            {"name": t.name, "description": t.description}
            for t in self._tools.values()
        ]

    # -- Lifecycle hooks --------------------------------------------------

    def on(self, event: str, name: str | None = None, description: str | None = None):
        """Decorator to register a lifecycle hook.

        Valid events: user_prompt_submit, pre_tool_use, post_tool_use, session_start.
        Hook return-value semantics are event-specific — see docs/AI.md.
        """
        if event not in HOOK_EVENTS:
            raise ValueError(
                f"Unknown hook event '{event}'. Valid events: {list(HOOK_EVENTS)}"
            )

        def decorator(func: Callable) -> Callable:
            hook_name = name or func.__name__
            hook_desc = description or (func.__doc__ or "").strip() or hook_name
            self._hooks.setdefault(event, []).append(
                HookDef(func, hook_name, event, hook_desc)
            )
            return func

        return decorator

    def list_hooks(self) -> list[dict]:
        """List all registered hooks with metadata, grouped-order by event."""
        out = []
        for event in HOOK_EVENTS:
            for h in self._hooks.get(event, []):
                out.append({
                    "name": h.name,
                    "event": h.event,
                    "description": h.description,
                })
        return out

    async def _call_hook(self, hook: "HookDef", payload: dict) -> Any:
        sig = python_inspect.signature(hook.func)
        kwargs = {k: v for k, v in payload.items() if k in sig.parameters}
        if python_inspect.iscoroutinefunction(hook.func):
            return await hook.func(**kwargs)
        return hook.func(**kwargs)

    async def run_hooks_first(
        self, event: str, enabled: set[str] | None, **payload
    ) -> Any:
        """Run hooks for `event`; return the first non-None result (gate semantics).

        Used by `pre_tool_use`: the first hook that returns a string blocks the tool
        and its return value is fed back to the LLM as the tool result.
        Exceptions are caught and logged — a raising hook does NOT deny.
        """
        for hook in self._hooks.get(event, []):
            if enabled is not None and hook.name not in enabled:
                continue
            try:
                result = await self._call_hook(hook, payload)
                if result is not None:
                    return result
            except Exception:
                logger.exception("Hook failed: event=%s name=%s", event, hook.name)
        return None

    async def run_hooks_pipe(
        self,
        event: str,
        enabled: set[str] | None,
        pipe_key: str,
        initial: Any,
        **payload,
    ) -> Any:
        """Run hooks for `event`, piping `initial` through each hook under `pipe_key`.

        Used by `user_prompt_submit` (piping `message`) and `post_tool_use`
        (piping `result`). Each hook sees the previous hook's output.
        Returning None leaves the piped value unchanged.
        """
        current = initial
        for hook in self._hooks.get(event, []):
            if enabled is not None and hook.name not in enabled:
                continue
            try:
                result = await self._call_hook(hook, {**payload, pipe_key: current})
                if result is not None:
                    current = result
            except Exception:
                logger.exception("Hook failed: event=%s name=%s", event, hook.name)
        return current

    async def run_hooks_fire(
        self, event: str, enabled: set[str] | None, **payload
    ) -> None:
        """Run hooks for `event`, ignoring return values (fire-and-forget).

        Used by `session_start`.
        """
        for hook in self._hooks.get(event, []):
            if enabled is not None and hook.name not in enabled:
                continue
            try:
                await self._call_hook(hook, payload)
            except Exception:
                logger.exception("Hook failed: event=%s name=%s", event, hook.name)


# Module-level singleton
tool_registry = ToolRegistry()


# ---------------------------------------------------------------------------
# Chat Handler
# ---------------------------------------------------------------------------


class AIChatHandler:
    """Manages chat sessions, history, and AI calls."""

    def __init__(self, provider: AIProvider, registry: ToolRegistry):
        self.provider = provider
        self.registry = registry

    async def chat(
        self,
        message: str,
        history: list[dict],
        system_prompt: str,
        enabled_tools: set[str] | None = None,
        enabled_hooks: set[str] | None = None,
        user: dict | None = None,
        db: Session | None = None,
        thinking: bool = False,
        progress_callback: Callable[[dict], Any] | None = None,
        images: list[str] | None = None,
    ) -> dict:
        """Process a chat message and return the response.

        Images are only attached to the current turn — historical turns are
        stored text-only by the endpoint, so cookie/session payloads stay
        small and the model never re-sees old base64 blobs.
        """
        # user_prompt_submit hooks: pipe the message through each hook.
        message = await self.registry.run_hooks_pipe(
            "user_prompt_submit",
            enabled_hooks,
            "message",
            message,
            history=history,
            user=user,
            db=db,
        )

        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.extend(history)
        if images:
            user_content: list[dict] = []
            if message:
                user_content.append({"type": "text", "text": message})
            for img in images:
                user_content.append({"type": "image_url", "image_url": {"url": img}})
            messages.append({"role": "user", "content": user_content})
        else:
            messages.append({"role": "user", "content": message})

        tools = self.registry.get_openai_tools(enabled_tools)
        result = await self.provider.chat(messages, tools=tools or None, thinking=thinking)

        tool_calls_made = []

        # Bounded agentic loop. Some models (notably Gemma when its
        # tool-call template misfires) return another tool call instead of
        # a summary on the post-tool turn. We iterate so the conversation
        # converges on a real answer rather than dead-ending in markup.
        MAX_TOOL_ITERATIONS = 6
        iteration = 0
        while result.get("tool_calls"):
            iteration += 1
            if iteration > MAX_TOOL_ITERATIONS:
                _log_diagnostic(
                    "tool_iteration_exceeded",
                    f"Stopped after {MAX_TOOL_ITERATIONS} tool iterations without a final answer",
                    last_response_len=len(result.get("response") or ""),
                )
                if not result.get("response"):
                    result["response"] = (
                        "The model kept requesting tools and didn't produce a final "
                        "answer after several attempts. The data has been gathered — "
                        "please try rephrasing your question."
                    )
                break

            if progress_callback:
                tool_names = [
                    tc.get("function", {}).get("name", "tool")
                    for tc in result["tool_calls"]
                ]
                ev = {
                    "type": "tool_iteration",
                    "iteration": iteration,
                    "max_iterations": MAX_TOOL_ITERATIONS,
                    "tools": tool_names,
                }
                cb_result = progress_callback(ev)
                if python_inspect.isawaitable(cb_result):
                    await cb_result

            # Add assistant message with tool calls
            messages.append({
                "role": "assistant",
                "content": result.get("response") or None,
                "tool_calls": result["tool_calls"],
            })

            for tc in result["tool_calls"]:
                func = tc["function"]
                try:
                    args = json.loads(func["arguments"]) if isinstance(func["arguments"], str) else func["arguments"]
                except json.JSONDecodeError:
                    args = {}

                # pre_tool_use gate: first hook returning a string blocks the call
                # and its value is fed back to the LLM as the tool result.
                blocked = await self.registry.run_hooks_first(
                    "pre_tool_use",
                    enabled_hooks,
                    tool_name=func["name"],
                    arguments=args,
                    user=user,
                    db=db,
                )
                if blocked is not None:
                    tool_result = str(blocked)
                else:
                    tool_result = await self.registry.execute(func["name"], args, db=db)
                    # post_tool_use: pipe result through hooks for transformation.
                    tool_result = await self.registry.run_hooks_pipe(
                        "post_tool_use",
                        enabled_hooks,
                        "result",
                        tool_result,
                        tool_name=func["name"],
                        arguments=args,
                        user=user,
                        db=db,
                    )

                tool_calls_made.append({
                    "name": func["name"],
                    "arguments": args,
                    "result": tool_result,
                    "blocked": blocked is not None,
                })

                messages.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": tool_result,
                })

            # Next turn — model should produce a final answer or another
            # round of tool calls (in which case we loop again).
            result = await self.provider.chat(messages, thinking=thinking)

        return {
            "response": result["response"],
            "tool_calls": tool_calls_made,
            "thinking": result.get("thinking", ""),
        }


# ---------------------------------------------------------------------------
# Settings Model
# ---------------------------------------------------------------------------


class AIChatSettings(Base):
    __tablename__ = "fasthx_admin_ai_settings"

    id = Column(Integer, primary_key=True)
    key = Column(String(255), unique=True, nullable=False)
    value = Column(Text)


class AIChatDiagnostic(Base):
    __tablename__ = "fasthx_admin_ai_diagnostics"

    id = Column(Integer, primary_key=True)
    ts = Column(DateTime(timezone=True), nullable=False, index=True)
    event_type = Column(String(64), nullable=False, index=True)
    detail = Column(String(1000), nullable=False, default="")
    extra = Column(Text, nullable=True)


AUTH_TYPE_API_KEY = "api_key"
AUTH_TYPE_KEYCLOAK = "keycloak"


class AIChatConnection(Base):
    __tablename__ = "fasthx_admin_ai_connections"

    id = Column(Integer, primary_key=True)
    name = Column(String(255), unique=True, nullable=False)
    base_url = Column(String(500), nullable=False, default="https://api.openai.com")
    api_key = Column(Text, default="")
    model = Column(String(255), nullable=False, default="gpt-4o-mini")
    temperature = Column(Float, nullable=False, default=0.7)
    max_tokens = Column(Integer, nullable=False, default=2048)
    timeout = Column(Float, nullable=False, default=60.0)
    ssl_verify = Column(Boolean, nullable=False, default=True)
    is_active = Column(Boolean, nullable=False, default=False)
    auth_type = Column(String(32), nullable=False, default=AUTH_TYPE_API_KEY)
    keycloak_url = Column(String(500), default="")
    keycloak_client_id = Column(String(255), default="")
    keycloak_client_secret = Column(Text, default="")


# Columns added to AIChatConnection after the initial release. Existing
# installations need an in-place ALTER to pick them up.
_CONNECTION_ADDITIVE_COLUMNS: list[tuple[str, str]] = [
    ("auth_type", "VARCHAR(32) NOT NULL DEFAULT 'api_key'"),
    ("keycloak_url", "VARCHAR(500) DEFAULT ''"),
    ("keycloak_client_id", "VARCHAR(255) DEFAULT ''"),
    ("keycloak_client_secret", "TEXT DEFAULT ''"),
]


def _ensure_connection_columns(engine) -> None:
    """Add columns to fasthx_admin_ai_connections that exist on the model but
    not in the live schema. SQLAlchemy's create_all only creates missing
    tables, not missing columns, so upgrades need this nudge.
    """
    inspector = inspect(engine)
    if not inspector.has_table(AIChatConnection.__tablename__):
        return
    existing = {col["name"] for col in inspector.get_columns(AIChatConnection.__tablename__)}
    for name, ddl in _CONNECTION_ADDITIVE_COLUMNS:
        if name in existing:
            continue
        with engine.begin() as conn:
            conn.execute(
                text(
                    f"ALTER TABLE {AIChatConnection.__tablename__} "
                    f"ADD COLUMN {name} {ddl}"
                )
            )


def ensure_ai_tables():
    """Create the AI settings tables if they don't exist."""
    engine = get_engine()
    AIChatSettings.__table__.create(bind=engine, checkfirst=True)
    AIChatConnection.__table__.create(bind=engine, checkfirst=True)
    AIChatDiagnostic.__table__.create(bind=engine, checkfirst=True)
    _ensure_connection_columns(engine)


def _get_settings(db: Session) -> dict[str, str]:
    """Load all AI settings from DB as a dict."""
    rows = db.query(AIChatSettings).all()
    return {row.key: row.value for row in rows}


def _save_settings(db: Session, settings: dict[str, str]):
    """Save settings dict to DB (upsert)."""
    for key, value in settings.items():
        row = db.query(AIChatSettings).filter(AIChatSettings.key == key).first()
        if row:
            row.value = value
        else:
            db.add(AIChatSettings(key=key, value=value))
    db.commit()


# ---------------------------------------------------------------------------
# In-memory session store (fallback when no session middleware)
# ---------------------------------------------------------------------------

_chat_sessions: dict[str, list[dict]] = {}
_MAX_HISTORY = 50


def _get_session_id(request: Request) -> str:
    """Get or create a chat session ID from cookies."""
    return request.cookies.get("fasthx_chat_sid", "")


def _get_history(session_id: str) -> list[dict]:
    if not session_id:
        return []
    history = _chat_sessions.get(session_id, [])
    return history[-_MAX_HISTORY:]


def _save_history(session_id: str, history: list[dict]):
    _chat_sessions[session_id] = history[-_MAX_HISTORY:]


# ---------------------------------------------------------------------------
# Settings cache
# ---------------------------------------------------------------------------

_settings_cache: dict[str, str] = {}
_settings_cache_time: float = 0
_CACHE_TTL = 30  # seconds


def _get_cached_settings(db: Session) -> dict[str, str]:
    global _settings_cache, _settings_cache_time
    now = time.time()
    if now - _settings_cache_time > _CACHE_TTL:
        _settings_cache = _get_settings(db)
        _settings_cache_time = now
    return _settings_cache


def _invalidate_settings_cache():
    global _settings_cache_time
    _settings_cache_time = 0


def is_chat_widget_enabled() -> bool:
    """Check if the AI chat widget is enabled in DB settings (uses cache)."""
    global _settings_cache, _settings_cache_time
    now = time.time()
    if now - _settings_cache_time > _CACHE_TTL or not _settings_cache:
        try:
            db = next(get_db())
            try:
                _settings_cache = _get_settings(db)
                _settings_cache_time = time.time()
            finally:
                db.close()
        except Exception:
            return False
    return _settings_cache.get("enabled") == "true"


# ---------------------------------------------------------------------------
# Router Factory
# ---------------------------------------------------------------------------


def _build_system_prompt(settings: dict[str, str]) -> str:
    """Build the full system prompt from base + context items."""
    base = settings.get("system_prompt", "You are a helpful admin assistant.")
    context_json = settings.get("context_items", "[]")
    try:
        context_items = json.loads(context_json)
    except (json.JSONDecodeError, TypeError):
        context_items = []

    parts = [base]
    for item in context_items:
        if item.get("enabled", True):
            parts.append(f"\n\n## {item['name']}\n{item['content']}")
    return "\n".join(parts)


_LEGACY_CONNECTION_KEYS = {
    "base_url", "api_key", "model", "temperature",
    "max_tokens", "timeout", "ssl_verify",
}


def _migrate_legacy_connection(db: Session) -> AIChatConnection | None:
    """If legacy per-model KV keys exist, seed a 'Default' connection from them.

    Why: earlier versions stored the single connection as KV rows. Preserve user
    config on upgrade so the chat keeps working without a manual reconfigure.
    """
    settings = _get_settings(db)
    if not any(k in settings for k in _LEGACY_CONNECTION_KEYS):
        return None

    conn = AIChatConnection(
        name="Default",
        base_url=settings.get("base_url", "https://api.openai.com"),
        api_key=settings.get("api_key", ""),
        model=settings.get("model", "gpt-4o-mini"),
        temperature=float(settings.get("temperature", "0.7") or 0.7),
        max_tokens=int(settings.get("max_tokens", "2048") or 2048),
        timeout=float(settings.get("timeout", "60") or 60),
        ssl_verify=settings.get("ssl_verify", "true") == "true",
        is_active=True,
        auth_type=AUTH_TYPE_API_KEY,
    )
    db.add(conn)

    for key in _LEGACY_CONNECTION_KEYS:
        row = db.query(AIChatSettings).filter(AIChatSettings.key == key).first()
        if row:
            db.delete(row)
    db.commit()
    return conn


def _get_active_connection(db: Session) -> AIChatConnection | None:
    """Return the active connection, migrating legacy KV state if needed."""
    conn = db.query(AIChatConnection).filter(AIChatConnection.is_active.is_(True)).first()
    if conn:
        return conn

    any_conn = db.query(AIChatConnection).order_by(AIChatConnection.id).first()
    if any_conn:
        any_conn.is_active = True
        db.commit()
        return any_conn

    return _migrate_legacy_connection(db)


def _set_active_connection(db: Session, conn_id: int) -> None:
    db.query(AIChatConnection).filter(AIChatConnection.is_active.is_(True)).update(
        {AIChatConnection.is_active: False}
    )
    target = db.query(AIChatConnection).filter(AIChatConnection.id == conn_id).first()
    if target:
        target.is_active = True
    db.commit()


def _build_provider(conn: AIChatConnection) -> AIProvider:
    """Build an AI provider from a connection row."""
    token_manager: KeycloakTokenManager | None = None
    if (conn.auth_type or AUTH_TYPE_API_KEY) == AUTH_TYPE_KEYCLOAK:
        if not conn.keycloak_url or not conn.keycloak_client_id:
            raise RuntimeError(
                f"Connection '{conn.name}' uses Keycloak auth but is missing "
                "keycloak_url or keycloak_client_id."
            )
        token_manager = KeycloakTokenManager(
            auth_url=conn.keycloak_url,
            client_id=conn.keycloak_client_id,
            client_secret=conn.keycloak_client_secret or "",
            ssl_verify=bool(conn.ssl_verify),
        )

    return OpenAICompatibleProvider(
        base_url=conn.base_url or "https://api.openai.com",
        api_key=conn.api_key or "",
        model=conn.model or "gpt-4o-mini",
        temperature=float(conn.temperature if conn.temperature is not None else 0.7),
        max_tokens=int(conn.max_tokens if conn.max_tokens is not None else 2048),
        timeout=float(conn.timeout if conn.timeout is not None else 60.0),
        ssl_verify=bool(conn.ssl_verify),
        token_manager=token_manager,
    )


async def ai_complete(
    prompt: str,
    *,
    system: str | None = None,
    tools: list[str] | None = None,
    db: Session | None = None,
) -> str:
    """One-shot prompt against the active AI connection. Returns response text.

    Stateless — no chat history, no hooks. Optionally allows the model to call
    tools registered via ``@tool_registry.tool()`` by passing their names in
    ``tools``. Tool execution is single-round: if the model emits tool calls,
    they run, results are fed back, and the model produces a final answer. A
    tool result that triggers another tool call will *not* fire — for
    agent-style loops use the chat handler.

    Raises RuntimeError if no connection is configured.
    """
    own_session = db is None
    if own_session:
        db = next(get_db())
    try:
        conn = _get_active_connection(db)
        if conn is None:
            raise RuntimeError(
                "No AI connection configured. Add one in AI Settings."
            )
        provider = _build_provider(conn)

        messages: list[dict] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        enabled = set(tools) if tools else None
        tool_specs = tool_registry.get_openai_tools(enabled) if enabled else None

        result = await provider.chat(messages, tools=tool_specs or None)

        if result.get("tool_calls"):
            messages.append({
                "role": "assistant",
                "content": result.get("response") or None,
                "tool_calls": result["tool_calls"],
            })
            for tc in result["tool_calls"]:
                func = tc["function"]
                try:
                    args = (
                        json.loads(func["arguments"])
                        if isinstance(func["arguments"], str)
                        else func["arguments"]
                    )
                except json.JSONDecodeError:
                    args = {}
                tool_result = await tool_registry.execute(func["name"], args, db=db)
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": tool_result,
                })
            final = await provider.chat(messages)
            return final["response"]

        return result["response"]
    finally:
        if own_session:
            db.close()


def create_ai_chat_router(admin) -> APIRouter:
    """Create FastAPI router with AI chat endpoints."""
    router = APIRouter(prefix="/ai", tags=["AI Chat"])
    templates = admin.templates

    @router.post("/chat")
    async def chat_endpoint(request: Request, db: Session = Depends(get_db)):
        settings = _get_cached_settings(db)
        if settings.get("enabled") != "true":
            return JSONResponse({"error": "AI chat is not enabled"}, status_code=400)

        body = await request.json()
        message = body.get("message", "").strip()
        thinking = bool(body.get("thinking", False))

        # Images arrive as data: URLs from the widget. Keep only well-formed
        # entries and cap count/size so a runaway client can't OOM the worker.
        raw_images = body.get("images") or []
        images: list[str] = []
        if isinstance(raw_images, list):
            for img in raw_images[:8]:
                if (
                    isinstance(img, str)
                    and img.startswith("data:image/")
                    and len(img) <= 8 * 1024 * 1024  # ~6 MB raw after base64 decode
                ):
                    images.append(img)

        if not message and not images:
            return JSONResponse({"error": "Empty message"}, status_code=400)
        # History placeholder when only images were sent — gives the model a
        # hint when this turn rolls into history on subsequent turns.
        history_text = message or "[Image]"

        session_id = _get_session_id(request)
        is_new_session = not session_id
        if is_new_session:
            session_id = str(uuid.uuid4())
        history = _get_history(session_id)

        conn = _get_active_connection(db)
        if conn is None:
            return JSONResponse(
                {"error": "No AI connection configured. Add one in AI Settings."},
                status_code=400,
            )
        provider = _build_provider(conn)
        system_prompt = _build_system_prompt(settings)

        # Determine enabled tools
        enabled_tools_json = settings.get("enabled_tools", "[]")
        try:
            enabled_tools = set(json.loads(enabled_tools_json))
        except (json.JSONDecodeError, TypeError):
            enabled_tools = set()

        # Determine enabled hooks
        enabled_hooks_json = settings.get("enabled_hooks", "[]")
        try:
            enabled_hooks = set(json.loads(enabled_hooks_json))
        except (json.JSONDecodeError, TypeError):
            enabled_hooks = set()

        # Resolve current user from the session (mock user when AUTH_DISABLED);
        # may still be None for an unauthenticated/expired session.
        user = get_current_user(request)

        # Fire session_start hooks on the first message of a new session.
        if is_new_session:
            await tool_registry.run_hooks_fire(
                "session_start",
                enabled_hooks,
                session_id=session_id,
                user=user,
                db=db,
            )

        handler = AIChatHandler(provider, tool_registry)

        wants_stream = "text/event-stream" in (request.headers.get("accept") or "")

        if not wants_stream:
            try:
                result = await handler.chat(
                    message, history, system_prompt,
                    enabled_tools=enabled_tools,
                    enabled_hooks=enabled_hooks,
                    user=user,
                    db=db,
                    thinking=thinking,
                    images=images,
                )
            except Exception as e:
                logger.exception("AI chat error")
                return JSONResponse({"error": str(e)}, status_code=500)

            history.append({"role": "user", "content": history_text})
            history.append({"role": "assistant", "content": result["response"]})
            _save_history(session_id, history)

            response = JSONResponse({
                "response": result["response"],
                "tool_calls": result["tool_calls"],
                "thinking": result.get("thinking", ""),
            })
            if not request.cookies.get("fasthx_chat_sid"):
                response.set_cookie("fasthx_chat_sid", session_id, httponly=True, samesite="lax")
            return response

        # --- SSE streaming path ---
        # Bridge the handler's progress_callback to an asyncio.Queue that the
        # streaming generator drains.  Sending None signals end-of-stream.
        queue: asyncio.Queue = asyncio.Queue()

        async def progress_cb(event: dict) -> None:
            await queue.put(event)

        async def run_chat_task() -> None:
            try:
                result = await handler.chat(
                    message, history, system_prompt,
                    enabled_tools=enabled_tools,
                    enabled_hooks=enabled_hooks,
                    user=user,
                    db=db,
                    thinking=thinking,
                    progress_callback=progress_cb,
                    images=images,
                )
                history.append({"role": "user", "content": history_text})
                history.append({"role": "assistant", "content": result["response"]})
                _save_history(session_id, history)
                await queue.put({
                    "type": "done",
                    "response": result["response"],
                    "tool_calls": result["tool_calls"],
                    "thinking": result.get("thinking", ""),
                })
            except Exception as e:
                logger.exception("AI chat error (stream)")
                await queue.put({"type": "error", "error": str(e)})
            finally:
                await queue.put(None)

        async def event_stream():
            chat_task = asyncio.create_task(run_chat_task())
            try:
                while True:
                    ev = await queue.get()
                    if ev is None:
                        break
                    yield f"data: {json.dumps(ev)}\n\n"
            finally:
                if not chat_task.done():
                    chat_task.cancel()

        headers = {
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # disable proxy buffering (nginx etc.)
        }
        stream_response = StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers=headers,
        )
        if not request.cookies.get("fasthx_chat_sid"):
            stream_response.set_cookie(
                "fasthx_chat_sid", session_id, httponly=True, samesite="lax"
            )
        return stream_response

    @router.post("/clear")
    async def clear_chat(request: Request):
        session_id = _get_session_id(request)
        if session_id:
            _chat_sessions.pop(session_id, None)
        return JSONResponse({"status": "ok"})

    @router.get("/history")
    async def get_history(request: Request):
        session_id = _get_session_id(request)
        history = _get_history(session_id)
        return JSONResponse({"messages": history})

    def _render_settings(request, db, save_success=False):
        _get_active_connection(db)  # trigger legacy migration if needed
        settings = _get_settings(db)
        connections = db.query(AIChatConnection).order_by(AIChatConnection.name).all()
        return templates.TemplateResponse("ai_settings.html", {
            "request": request,
            "settings": settings,
            "connections": connections,
            "active_page": "ai_settings",
            "save_success": save_success,
        })

    @router.get("/settings", response_class=HTMLResponse)
    async def settings_page(request: Request, db: Session = Depends(get_db)):
        return _render_settings(request, db)

    @router.post("/settings", response_class=HTMLResponse)
    async def save_settings(request: Request, db: Session = Depends(get_db)):
        form = await request.form()
        settings_to_save = {
            "enabled": "true" if form.get("enabled") else "false",
            "system_prompt": str(form.get("system_prompt", "")),
        }
        _save_settings(db, settings_to_save)
        _invalidate_settings_cache()
        return _render_settings(request, db, save_success=True)

    def _render_connection_form(request, conn=None, error=None):
        return templates.TemplateResponse("ai_connection_form.html", {
            "request": request,
            "connection": conn,
            "error": error,
            "active_page": "ai_settings",
        })

    def _connection_from_form(form, existing: AIChatConnection | None) -> tuple[AIChatConnection | None, str | None]:
        name = (form.get("name") or "").strip()
        if not name:
            return None, "Name is required."

        conn = existing or AIChatConnection()
        conn.name = name
        conn.base_url = (form.get("base_url") or "https://api.openai.com").strip()
        api_key = form.get("api_key", "")
        if api_key != "********":
            conn.api_key = api_key
        conn.model = (form.get("model") or "gpt-4o-mini").strip()
        try:
            conn.temperature = float(form.get("temperature") or 0.7)
            conn.max_tokens = int(form.get("max_tokens") or 2048)
            conn.timeout = float(form.get("timeout") or 60)
        except ValueError:
            return None, "Temperature, max tokens, and timeout must be numeric."
        conn.ssl_verify = form.get("ssl_verify") == "on"

        auth_type = (form.get("auth_type") or AUTH_TYPE_API_KEY).strip()
        if auth_type not in (AUTH_TYPE_API_KEY, AUTH_TYPE_KEYCLOAK):
            return None, f"Unknown auth type: {auth_type}"
        conn.auth_type = auth_type

        conn.keycloak_url = (form.get("keycloak_url") or "").strip()
        conn.keycloak_client_id = (form.get("keycloak_client_id") or "").strip()
        kc_secret = form.get("keycloak_client_secret", "")
        if kc_secret != "********":
            conn.keycloak_client_secret = kc_secret

        if auth_type == AUTH_TYPE_KEYCLOAK:
            if not conn.keycloak_url:
                return None, "Keycloak token URL is required for Keycloak auth."
            if not conn.keycloak_client_id:
                return None, "Keycloak client ID is required for Keycloak auth."
            if not conn.keycloak_client_secret:
                return None, "Keycloak client secret is required for Keycloak auth."

        return conn, None

    @router.get("/settings/connections/new", response_class=HTMLResponse)
    async def new_connection(request: Request):
        return _render_connection_form(request)

    @router.post("/settings/connections")
    async def create_connection(request: Request, db: Session = Depends(get_db)):
        form = await request.form()
        conn, error = _connection_from_form(form, None)
        if error:
            return _render_connection_form(request, error=error)
        # First connection auto-activates
        if db.query(AIChatConnection).count() == 0:
            conn.is_active = True
        try:
            db.add(conn)
            db.commit()
        except Exception as e:
            db.rollback()
            return _render_connection_form(request, error=f"Could not save: {e}")
        _invalidate_settings_cache()
        return RedirectResponse("/ai/settings", status_code=303)

    @router.get("/settings/connections/{conn_id}/edit", response_class=HTMLResponse)
    async def edit_connection(conn_id: int, request: Request, db: Session = Depends(get_db)):
        conn = db.query(AIChatConnection).filter(AIChatConnection.id == conn_id).first()
        if not conn:
            return HTMLResponse("Connection not found", status_code=404)
        return _render_connection_form(request, conn=conn)

    @router.post("/settings/connections/{conn_id}")
    async def update_connection(conn_id: int, request: Request, db: Session = Depends(get_db)):
        conn = db.query(AIChatConnection).filter(AIChatConnection.id == conn_id).first()
        if not conn:
            return HTMLResponse("Connection not found", status_code=404)
        form = await request.form()
        updated, error = _connection_from_form(form, conn)
        if error:
            return _render_connection_form(request, conn=conn, error=error)
        try:
            db.commit()
        except Exception as e:
            db.rollback()
            return _render_connection_form(request, conn=conn, error=f"Could not save: {e}")
        _invalidate_settings_cache()
        return RedirectResponse("/ai/settings", status_code=303)

    @router.post("/settings/connections/{conn_id}/activate")
    async def activate_connection(conn_id: int, db: Session = Depends(get_db)):
        _set_active_connection(db, conn_id)
        _invalidate_settings_cache()
        return RedirectResponse("/ai/settings", status_code=303)

    @router.post("/settings/connections/{conn_id}/delete")
    async def delete_connection(conn_id: int, db: Session = Depends(get_db)):
        conn = db.query(AIChatConnection).filter(AIChatConnection.id == conn_id).first()
        if conn:
            was_active = conn.is_active
            db.delete(conn)
            db.commit()
            if was_active:
                fallback = db.query(AIChatConnection).order_by(AIChatConnection.id).first()
                if fallback:
                    fallback.is_active = True
                    db.commit()
        _invalidate_settings_cache()
        return RedirectResponse("/ai/settings", status_code=303)

    @router.get("/diagnostics", response_class=HTMLResponse)
    async def diagnostics_page(request: Request, db: Session = Depends(get_db)):
        return templates.TemplateResponse("ai_diagnostics.html", {
            "request": request,
            "events": get_diagnostics(db),
            "active_page": "ai_diagnostics",
        })

    @router.post("/diagnostics/clear")
    async def diagnostics_clear(db: Session = Depends(get_db)):
        clear_diagnostics(db)
        return RedirectResponse("/ai/diagnostics", status_code=303)

    @router.get("/settings/context", response_class=HTMLResponse)
    async def context_settings_page(request: Request, db: Session = Depends(get_db)):
        settings = _get_settings(db)
        context_items = []
        try:
            context_items = json.loads(settings.get("context_items", "[]"))
        except (json.JSONDecodeError, TypeError):
            pass

        enabled_tools: list[str] = []
        try:
            enabled_tools = json.loads(settings.get("enabled_tools", "[]"))
        except (json.JSONDecodeError, TypeError):
            pass

        enabled_hooks: list[str] = []
        try:
            enabled_hooks = json.loads(settings.get("enabled_hooks", "[]"))
        except (json.JSONDecodeError, TypeError):
            pass

        return templates.TemplateResponse("ai_context_settings.html", {
            "request": request,
            "context_items": context_items,
            "tools": tool_registry.list_tools(),
            "enabled_tools": enabled_tools,
            "hooks": tool_registry.list_hooks(),
            "enabled_hooks": enabled_hooks,
            "active_page": "ai_context_settings",
        })

    @router.post("/settings/context", response_class=HTMLResponse)
    async def save_context_settings(request: Request, db: Session = Depends(get_db)):
        form = await request.form()

        # Parse context items from form
        context_items = []
        idx = 0
        while True:
            name = form.get(f"context_name_{idx}")
            if name is None:
                break
            content = form.get(f"context_content_{idx}", "")
            enabled = form.get(f"context_enabled_{idx}") == "on"
            if name.strip():
                context_items.append({
                    "name": name.strip(),
                    "content": content,
                    "enabled": enabled,
                })
            idx += 1

        # Parse enabled tools
        enabled_tools = []
        for tool_info in tool_registry.list_tools():
            if form.get(f"tool_{tool_info['name']}") == "on":
                enabled_tools.append(tool_info["name"])

        # Parse enabled hooks
        enabled_hooks = []
        for hook_info in tool_registry.list_hooks():
            if form.get(f"hook_{hook_info['name']}") == "on":
                enabled_hooks.append(hook_info["name"])

        _save_settings(db, {
            "context_items": json.dumps(context_items),
            "enabled_tools": json.dumps(enabled_tools),
            "enabled_hooks": json.dumps(enabled_hooks),
        })
        _invalidate_settings_cache()

        settings = _get_settings(db)
        return templates.TemplateResponse("ai_context_settings.html", {
            "request": request,
            "context_items": context_items,
            "tools": tool_registry.list_tools(),
            "enabled_tools": enabled_tools,
            "hooks": tool_registry.list_hooks(),
            "enabled_hooks": enabled_hooks,
            "active_page": "ai_context_settings",
            "save_success": True,
        })

    return router
