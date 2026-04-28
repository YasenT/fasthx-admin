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
import inspect as python_inspect
import json
import logging
import time
import uuid
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, get_type_hints

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import Boolean, Column, Float, Integer, String, Text, inspect, text
from sqlalchemy.orm import Session

from .database import Base, get_db, get_engine

logger = logging.getLogger("fasthx_admin.ai_chat")

_PACKAGE_DIR = Path(__file__).resolve().parent


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

        async with httpx.AsyncClient(timeout=self.timeout, verify=self.ssl_verify) as client:
            resp = await client.post(url, json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()

        choice = data["choices"][0]
        message = choice["message"]
        tool_calls = message.get("tool_calls")

        return {
            "response": message.get("content") or "",
            "tool_calls": tool_calls,
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
    ) -> dict:
        """Process a chat message and return the response."""
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
        messages.append({"role": "user", "content": message})

        tools = self.registry.get_openai_tools(enabled_tools)
        result = await self.provider.chat(messages, tools=tools or None)

        tool_calls_made = []

        # Handle tool calls
        if result.get("tool_calls"):
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

            # Get final response after tool calls
            final = await self.provider.chat(messages)
            result["response"] = final["response"]

        return {
            "response": result["response"],
            "tool_calls": tool_calls_made,
        }


# ---------------------------------------------------------------------------
# Settings Model
# ---------------------------------------------------------------------------


class AIChatSettings(Base):
    __tablename__ = "fasthx_admin_ai_settings"

    id = Column(Integer, primary_key=True)
    key = Column(String(255), unique=True, nullable=False)
    value = Column(Text)


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
        if not message:
            return JSONResponse({"error": "Empty message"}, status_code=400)

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

        # Resolve current user (may be None if auth disabled/unresolved)
        user = getattr(request.state, "user", None)

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
        try:
            result = await handler.chat(
                message, history, system_prompt,
                enabled_tools=enabled_tools,
                enabled_hooks=enabled_hooks,
                user=user,
                db=db,
            )
        except Exception as e:
            logger.exception("AI chat error")
            return JSONResponse({"error": str(e)}, status_code=500)

        # Update history
        history.append({"role": "user", "content": message})
        history.append({"role": "assistant", "content": result["response"]})
        _save_history(session_id, history)

        response = JSONResponse({
            "response": result["response"],
            "tool_calls": result["tool_calls"],
        })
        if not request.cookies.get("fasthx_chat_sid"):
            response.set_cookie("fasthx_chat_sid", session_id, httponly=True, samesite="lax")
        return response

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
