# AI Chat (Optional)

[← Back to README](../README.md)

fasthx-admin ships with an optional AI chat widget that adds a floating assistant to every page. It supports any OpenAI-compatible API (OpenAI, vLLM, Ollama, LiteLLM, etc.), a decorator-based tool registry so the AI can call your Python functions, and a settings UI stored in the database.

## Table of Contents

- [Enabling AI Chat](#enabling-ai-chat)
- [Installing the AI Dependency](#installing-the-ai-dependency)
- [Registering Tools](#registering-tools)
- [Configuring via the Settings UI](#configuring-via-the-settings-ui)
- [How It Works](#how-it-works)
- [Custom Providers](#custom-providers)
- [AI Chat API Endpoints](#ai-chat-api-endpoints)
- [Lifecycle Hooks](#lifecycle-hooks)

---

## Enabling AI Chat

Pass `ai_chat=True` when creating the Admin instance:

```python
from fasthx_admin import Admin

admin = Admin(app, title="My Admin", ai_chat=True)
```

This automatically:
- Creates a `fasthx_admin_ai_settings` table in your database
- Mounts chat API endpoints under `/ai/`
- Adds "AI Settings" and "AI Context & Tools" links in the sidebar under a "Settings" category
- Includes the chat widget on every page (once enabled in settings)

## Installing the AI Dependency

The AI chat uses `httpx` for async HTTP calls to the LLM API. Install it via the `ai` extra:

```bash
pip install fasthx-admin[ai]
```

If you already have `httpx` installed (e.g. from the `dev` extra), no additional install is needed.

## Registering Tools

Tools let the AI call your Python functions to answer questions with live data. Use the `@tool_registry.tool()` decorator:

```python
from fasthx_admin import tool_registry

@tool_registry.tool(description="Get the total number of customers")
def customer_count(db=None):
    """Returns the total number of customers."""
    count = db.query(Customer).count()
    return f"There are {count} customers."

@tool_registry.tool(description="Look up a customer by name")
def find_customer(name: str, db=None):
    """Find a customer by name (partial match)."""
    results = db.query(Customer).filter(Customer.name.ilike(f"%{name}%")).all()
    if not results:
        return f"No customers found matching '{name}'."
    return "\n".join(f"- {c.name} (SID: {c.sid})" for c in results)

@tool_registry.tool(description="Get edge device statistics")
async def edge_stats(db=None):
    """Returns edge device status breakdown."""
    total = db.query(FortiEdge).count()
    return f"Total edges: {total}"
```

Key points:
- **`db` parameter** -- if your function accepts a `db` parameter, it automatically receives the current SQLAlchemy session
- **Async support** -- tools can be `async def` or regular `def`
- **Type hints** -- parameter types (str, int, float, bool) are extracted and sent to the AI in OpenAI function-calling format
- **Return a string** -- the return value is sent back to the AI as the tool result
- Tools must be **enabled** in the settings UI before the AI can use them

## Configuring via the Settings UI

After enabling `ai_chat=True`, navigate to **Settings > AI Settings** in the sidebar. The settings page has four sections:

| Section | Fields | Description |
|---|---|---|
| **General** | Enable/disable toggle | Master switch for the chat widget |
| **Connection** | Base URL, API key, model | Your LLM endpoint (e.g. `https://api.openai.com`, `http://localhost:11434`) |
| **Parameters** | Temperature, max tokens, timeout | Generation parameters |
| **System Prompt** | Large text area | Base instructions for the AI |

The **Context & Tools** page (linked from the settings page) lets you:
- Add **context items** -- named text segments injected into the system prompt (e.g. business rules, schema descriptions)
- Toggle context items on/off
- Enable/disable registered **tools** individually

All settings are stored in the `fasthx_admin_ai_settings` database table as key-value pairs.

## How It Works

```
User types message in chat widget
    → POST /ai/chat {message: "..."}
    → Load settings from DB (cached 30s)
    → Build system prompt (base + enabled context items)
    → Load session history (in-memory, keyed by cookie)
    → Call LLM provider with messages + enabled tools
    → If AI requests tool calls:
        → Execute tools via registry (with DB session)
        → Send tool results back to AI
        → Get final response
    → Save to session history (max 50 messages)
    → Return {response, tool_calls_made}
```

- **Session history** is stored in-memory on the server, keyed by a `fasthx_chat_sid` cookie
- History persists across page navigations but resets on server restart
- The chat widget renders markdown responses using `marked.js` + `DOMPurify` (loaded from CDN)
- Widget size and expanded/minimized state persist in `localStorage`

## Custom Providers

The built-in `OpenAICompatibleProvider` works with any API that speaks the OpenAI chat completions format. To integrate a different API, subclass `AIProvider`:

```python
from fasthx_admin import AIProvider

class MyCustomProvider(AIProvider):
    name = "my_provider"

    async def chat(self, messages, tools=None, **kwargs):
        # Call your LLM API here
        # Must return: {"response": str, "tool_calls": list | None}
        ...

    def get_config_fields(self):
        # Return list of settings fields for the UI
        return [
            {"key": "api_key", "label": "API Key", "type": "password", "default": ""},
        ]
```

## AI Chat API Endpoints

All endpoints are mounted under `/ai/`:

| Method | Path | Description |
|---|---|---|
| `POST` | `/ai/chat` | Send a message, get AI response (JSON) |
| `POST` | `/ai/clear` | Clear the current session's chat history |
| `GET` | `/ai/history` | Get the current session's message history (JSON) |
| `GET` | `/ai/settings` | AI settings page (HTML) |
| `POST` | `/ai/settings` | Save AI settings |
| `GET` | `/ai/settings/context` | Context & tools settings page (HTML) |
| `POST` | `/ai/settings/context` | Save context items and tool toggles |

The `POST /ai/chat` endpoint expects JSON `{"message": "..."}` and returns:

```json
{
    "response": "The AI's markdown response",
    "tool_calls": [
        {"name": "customer_count", "arguments": {}, "result": "There are 4 customers."}
    ]
}
```

## Lifecycle Hooks

Lifecycle hooks are event handlers that fire at specific stages of the chat flow. Use them for audit logging, dynamic context injection, tool gating, or transforming tool results before they reach the AI.

Hooks are **registered in Python code** (not in the UI — the hook body is arbitrary Python, so a DB-backed textarea would be a remote code execution hole) and **toggled on/off in the settings UI** alongside tools.

### Events

| Event | Fires | Payload args your hook may accept | Return semantics |
|---|---|---|---|
| `user_prompt_submit` | Before the user's message is sent to the LLM | `message, history, user, db` | Return a `str` to replace the message; `None` = unchanged. Hooks chain — each sees the previous hook's output. |
| `pre_tool_use` | Before each tool call | `tool_name, arguments, user, db` | Return a `str` to **block** the call — the string is fed back to the LLM as the tool result so the AI can react. `None` = allow. First non-`None` wins. |
| `post_tool_use` | After each tool call | `tool_name, arguments, result, user, db` | Return a `str` to transform the result; `None` = unchanged. Hooks chain. |
| `session_start` | On the first message of a new chat session | `session_id, user, db` | Return value ignored (fire-and-forget). |

Hook functions only need to declare the args they actually use — the framework filters by signature.

### Registering hooks

```python
from fasthx_admin import tool_registry
from datetime import date

@tool_registry.on("session_start", description="Log when a new session starts")
def audit_session(session_id, user):
    logger.info("chat session started sid=%s user=%s", session_id, user)

@tool_registry.on("user_prompt_submit", description="Inject today's date")
def inject_today(message):
    return f"{message}\n\n(Today is {date.today().isoformat()})"

@tool_registry.on("pre_tool_use", description="Admin-only deletes")
def gate_deletes(tool_name, user):
    if tool_name.startswith("delete_") and (not user or user.get("role") != "admin"):
        return f"Blocked: only admins may call {tool_name}."

@tool_registry.on("post_tool_use", description="Redact email addresses from results")
def redact_emails(result):
    import re
    return re.sub(r"[\w.+-]+@[\w.-]+", "[redacted]", result) if isinstance(result, str) else None
```

Each hook is registered with a name (defaults to `func.__name__`) and shows up in the settings UI under **Settings > AI Context & Tools > Lifecycle Hooks**, grouped by event. Only hooks that are enabled in the settings UI will fire.

### The gate pattern

`pre_tool_use` hooks are the most powerful. Returning a string from one blocks the tool call and feeds that string back to the LLM **as if it were the tool's output**. This is modeled on Claude Code's exit-code-2 hook convention — the AI sees a denial it can reason about and adapt, rather than a hard error.

```python
@tool_registry.on("pre_tool_use")
def rate_limit(tool_name, user, db):
    recent = db.query(ToolCallLog).filter_by(user=user["username"]).count()
    if recent > 10:
        return "Rate limit exceeded — please wait a minute before calling more tools."
```

### Error handling

Hooks that raise an exception are caught and logged; they do **not** deny the action (fail-open). This is intentional — a broken hook should not brick the chat. To block a tool, return a string. To surface an error, log it inside the hook and return a user-facing message.

### Async support

Hooks can be `async def` or regular `def`. The framework awaits coroutines automatically, same as tools.

### Example output

With the `gate_deletes` and `inject_today` hooks enabled, a conversation looks like:

```
User: what time is it and can you delete customer #42?
  → user_prompt_submit injects date
  → AI calls delete_customer(id=42)
  → pre_tool_use returns "Blocked: only admins may call delete_customer."
  → AI sees the denial as tool output and responds:
    "Today's date is 2026-04-22. I can't delete that customer — only admins can."
```
