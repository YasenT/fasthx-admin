# fasthx-admin — System Specification

> Architectural blueprint for AI assistants working on this codebase.
> This document codifies the program's core architecture, design principles, and invariants.
> All modifications must be evaluated against this specification.

## Identity

**fasthx-admin** is a pip-installable admin interface framework for FastAPI. It generates full CRUD interfaces from SQLAlchemy models using HTMX for interactivity and Jinja2 for server-side rendering. It replaces Flask-Admin with a modern, composable architecture.

- **Package:** `fasthx-admin` (PyPI)
- **Build:** Hatchling, src layout (`src/fasthx_admin/`)
- **Python:** 3.9+
- **License:** MIT
- **Remote:** `git@github.com:talbiston/fasthx-admin.git`

---

## Non-Negotiable Rules

1. **No client-side frameworks.** All rendering is server-side via Jinja2. HTMX handles dynamic updates. No React, Vue, or similar. JavaScript is minimal and purposeful.
2. **SQLAlchemy is the only ORM.** All models inherit from `Base`. All queries use SQLAlchemy's API with parameterized queries. No raw SQL.
3. **FastAPI is the only web framework.** Routes use FastAPI's `APIRouter`. Dependencies use `Depends()`. No Flask, Django, or Starlette patterns.
4. **Convention over configuration.** CRUDView auto-generates everything from model introspection. Users override only what they need.
5. **No breaking the public API.** Exports in `__init__.py` are the contract. Adding is fine; removing or renaming requires a major version bump.
6. **Templates must auto-escape.** Jinja2's auto-escaping is always on. `column_formatters` that return HTML are explicitly trusted — this is by design, not a bug.
7. **No unnecessary dependencies.** Core deps are minimal. Optional features (AI chat, Excel export) use optional deps with clear error messages if missing.

---

## Module Architecture

```
src/fasthx_admin/
├── __init__.py        # Public API surface — all exports defined here
├── database.py        # SQLAlchemy engine/session globals (init_db, get_db, get_engine, Base)
├── auth.py            # OIDC/Keycloak auth (oidc_login, get_current_user, AuthError)
├── crud.py            # Core engine: CRUDView, Admin, ValidationError, toast_response
├── ai_chat.py         # Optional AI chat: providers, tool registry, settings, chat handler
├── templates/         # Jinja2 templates (base, list, form, detail, wizard, AI settings)
│   └── partials/      # Reusable template fragments (form fields, table body, row actions)
└── static/            # CSS (dark/light theme) and JS (HTMX handlers, theme toggle, Tom Select)
```

### Module Responsibilities

| Module | Role | Key Exports |
|--------|------|-------------|
| `database.py` | Global SQLAlchemy configuration | `Base`, `init_db()`, `get_db()`, `get_engine()` |
| `auth.py` | OIDC authentication flow | `oidc_login()`, `get_current_user()`, `AuthError`, `AUTH_DISABLED` |
| `crud.py` | CRUD generation, routing, Admin factory | `CRUDView`, `Admin`, `ValidationError`, `toast_response()`, `COLUMN_TYPE_MAP` |
| `ai_chat.py` | Pluggable AI chat with tools | `AIProvider`, `OpenAICompatibleProvider`, `ToolRegistry`, `tool_registry` |

### Dependency Flow

```
database.py ← auth.py ← crud.py ← ai_chat.py
                              ↑
                         __init__.py (re-exports)
```

Modules must not create circular imports. `database.py` depends on nothing internal. `auth.py` depends only on `database.py`. `crud.py` depends on both. `ai_chat.py` depends on `database.py`.

---

## Core Concepts

### The Admin Factory

`Admin` is the orchestrator. It:
1. Owns the `Jinja2Templates` instance
2. Mounts static files at `/static/fasthx-admin/`
3. Wraps `TemplateResponse` to inject global context (nav, auth, settings) into every response
4. Registers `CRUDView` instances via `add_view()`
5. Builds sidebar navigation from view categories

**The TemplateResponse wrapper is the central integration point.** It monkey-patches the templates object so that every template automatically receives: `current_user`, `nav_categories`, `active_page`, `static_url`, `admin_title`, `ai_chat_enabled`. It also enforces auth — unauthenticated requests to non-public pages redirect to `/login`.

### The CRUDView Engine

`CRUDView` is the core abstraction. A subclass with `model = SomeModel` automatically gets:
- List view with search, sort, filter, pagination, export
- Create/Edit forms with type-appropriate widgets
- Detail view
- Delete action
- HTMX polling columns
- AJAX foreign-key selects

**Initialization flow:**
1. Validate `model` is set
2. Resolve display metadata from model's `__admin_name__`, `__admin_category__`, `__admin_icon__`
3. Introspect SQLAlchemy model → extract columns, relationships, foreign keys
4. Register model in global `_model_registry`
5. Build `columns_meta` (list view) and `form_fields` (form view) from introspection
6. Auto-register all route handlers on `self.router`
7. Call `setup_endpoints()` for user-defined custom routes

**Column type mapping** (`COLUMN_TYPE_MAP`) translates SQLAlchemy column types to HTML input types. This mapping uses uppercase type names (e.g., `"INTEGER"` → `"number"`, `"VARCHAR"` → `"text"`).

### Model Metadata Protocol

SQLAlchemy models communicate with the admin via class attributes:

```python
class MyModel(Base):
    __tablename__ = "my_model"
    __admin_name__ = "My Model"        # Display name in UI
    __admin_category__ = "Management"  # Sidebar group
    __admin_icon__ = "gear"            # Bootstrap icon name
```

These are optional. Defaults: name from table name, category from view, icon `"table"`.

### The Toast Notification System

`toast_response()` creates HTML responses that trigger client-side toast notifications via HTMX's `HX-Trigger` header mechanism. For redirects, toast data is stored in a cookie (`_toast`) that survives full-page navigation. The client JS reads and clears this cookie.

---

## Data Flows

### Create/Edit Flow

```
GET /{name}/create or /{name}/{id}/edit
  → _prepare_form_fields(db, item?) → populate field metadata + current values + FK options
  → Render form.html

POST /{name}/create or /{name}/{id}/edit
  → Parse multipart form data
  → Instantiate model (create) or fetch existing (edit)
  → _apply_form_data(item, form_data) → type coercion per column type
  → validate(item, form_data, is_new) → user override hook, raises ValidationError
  → db.add() + db.commit()
  → Success: HX-Redirect to list view
  → ValidationError: rollback, re-render form with error toast
  → IntegrityError: rollback, generic error toast
```

### List/Search/Filter Flow

```
GET /{name}?partial=1&q=term&flt0_col_op=value&sort=col&order=asc&page=2
  → _parse_filter_params() → extract filter tuples from URL
  → _safe_build_query() → SQLAlchemy query with search (OR across searchable cols),
    filters, sort, pagination
  → HTMX request: return table_body.html partial (rows only)
  → Regular request: return full list.html
```

**Filter URL encoding:** `flt{index}_{column}_{operation}={value}`

### Auth Flow

```
POST /login (username, password)
  → oidc_login() → token endpoint → userinfo → group check
  → Success: store user in session, redirect to /
  → AuthError: re-render login with error

Every TemplateResponse:
  → Admin wrapper checks get_current_user()
  → If None and template not in public_pages: redirect to /login
  → AUTH_DISABLED=1: returns mock user {"username": "dev"}
```

---

## Extension Points

These are the sanctioned ways users customize behavior. New features should integrate through these mechanisms, not invent new ones unless its absolutly needed.

| Mechanism | Purpose | How |
|-----------|---------|-----|
| `validate(item, form_data, is_new)` | Custom validation before save | Override method, raise `ValidationError` |
| `setup_endpoints()` | Register custom routes | Override method, add routes to `self.router` |
| `@CRUDView.endpoint()` | Declarative custom endpoints | Decorator on view methods |
| `column_formatters` | Custom cell rendering | `Dict[col, callable(value, item) -> str]` |
| `form_widget_overrides` | Override field type/choices | `Dict[col, {type, choices, ...}]` |
| `form_sections` | Group form fields | `Dict[section_name, [col_keys]]` |
| `form_ajax_refs` | AJAX foreign-key selects | `Dict[col, {model, fields, page_size}]` |
| `row_actions` | Table row action buttons | `List[{label, icon, hx_post, ...}]` |
| `htmx_columns` | Polling cell updates | `Dict[col, {url, trigger}]` |
| `column_filters` | Filterable columns | `List[col_key]` |
| `export_types` | Data export formats | `List["csv", "xlsx"]` |
| `extra_templates_dirs` | Custom/override templates | Passed to `Admin()` constructor |
| `@tool_registry.tool()` | AI chat tools | Decorator on functions with type hints |
| `AIProvider` subclass | Custom AI backend | Implement `chat()` and `get_config_fields()` |

---

## CRUDView Configuration Reference

All attributes are class-level. Set them on your CRUDView subclass.

```python
# Required
model = None                    # SQLAlchemy model class

# Identity
name = None                     # URL slug (default: table name)
display_name = None             # UI label (default: __admin_name__ or table name)
category = None                 # Sidebar group (default: __admin_category__)
icon = None                     # Bootstrap icon (default: __admin_icon__ or "table")

# List view
column_list = None              # Columns to show (default: all)
column_exclude = None           # Columns to hide
column_labels = None            # Dict[col, display_label]
column_formatters = None        # Dict[col, callable(value, item) -> str]
column_searchable = None        # Searchable columns (default: auto-detect strings)
column_sortable = None          # Sortable columns (default: all)
column_filters = None           # Filterable columns
page_size = 20                  # Rows per page

# Form view
form_columns = None             # Fields to show (default: all except pk)
form_sections = None            # Dict[section, [fields]] for accordion layout
form_widget_overrides = None    # Dict[col, {type, choices, ...}]
form_ajax_refs = None           # Dict[col, {model, fields, page_size}]

# Permissions
can_create = True
can_edit = True
can_delete = True

# Advanced
pk_field = "id"                 # Primary key column name
row_actions = None              # Custom row action buttons
htmx_columns = None             # Polling columns
export_types = None             # Export formats
list_template = "list.html"     # Override list template
create_template = "form.html"   # Override create template
edit_template = "form.html"     # Override edit template
```

---

## Invariants

These conditions must always hold. Violating any of these is a bug.

### Database
- `init_db()` must be called exactly once before any route handles a request
- All models must inherit from `Base`
- Every model used with CRUDView must have a primary key column (default `"id"`)
- `get_db()` always yields a session and closes it in its finally block

### Views
- `CRUDView.model` must be set to a valid SQLAlchemy model class
- Column names referenced in any CRUDView attribute must exist in the model
- `_model_registry` is populated during CRUDView `__init__` — FK resolution depends on this
- `_apply_form_data()` never commits — the caller is responsible for commit/rollback

### Templates
- Every `TemplateResponse` passes through Admin's wrapper (which injects global context)
- Templates must receive `request` in their context (Jinja2/Starlette requirement)
- Asset paths use `{{ static_url }}` — never hardcode `/static/` paths
- `base.html` defines blocks: `title`, `page_title`, `content`, `extra_js`

### HTMX Contract
- List view search/filter/pagination returns `table_body.html` partial when `?partial=1` and `HX-Request` header present
- Form submissions return `HX-Redirect` header on success, re-rendered form on failure
- Row actions use `hx-post`/`hx-get` with `hx-target` for partial updates
- Toast notifications use `HX-Trigger: {"showToast": {...}}` header

### Authentication
- In production (`AUTH_DISABLED` not set): all non-public templates require authenticated session
- `public_pages` defaults to `{"login.html"}`
- `get_current_user()` returns `dict | None` — never raises

### AI Chat (when enabled)
- `AIProvider.chat()` must return `{response: str, tool_calls: list | None}`
- Tool functions must have type hints for parameter extraction
- Only tools in `enabled_tools` set are passed to the provider
- Chat history is per-session (cookie `fasthx_chat_sid`), capped at 50 messages
- Settings are cached with 30-second TTL

---

## Error Handling Patterns

| Context | Exception | Behavior |
|---------|-----------|----------|
| Form validation | `ValidationError` | Rollback, re-render form with error toast |
| DB constraint | `IntegrityError` | Rollback, generic error toast |
| Item not found | Query returns `None` | 404 HTMLResponse |
| Permission denied | `can_create/edit/delete=False` | 403 HTMLResponse |
| Auth failure | `AuthError` | Re-render login with message |
| AI chat error | `Exception` | JSON 500 response, logged |
| Missing optional dep | `ImportError` | Clear message naming the package to install |

**Pattern:** Always rollback before re-rendering. Never let a failed transaction leak.

---

## Design Principles

1. **Server-rendered first.** The server produces complete HTML. HTMX progressively enhances with partial updates. The app works without JavaScript for basic navigation.

2. **Model-driven UI.** The SQLAlchemy model is the single source of truth. Column types determine input widgets, validation rules, and filter operations. Don't duplicate schema information.

3. **Composition over inheritance.** `Admin` composes `CRUDView` instances. Views are configured via class attributes, not deep inheritance hierarchies. One level of subclassing is the norm.

4. **Explicit over magic.** Users set `column_list`, `form_sections`, etc. explicitly. Auto-detection (searchable columns, FK options) provides sensible defaults that can be overridden.

5. **Progressive disclosure.** A minimal CRUDView (just `model = X`) works out of the box. Advanced features (filters, formatters, AJAX refs, polling, AI) are opt-in.

6. **Keep the package small.** This is a library, not a platform. Resist scope creep. Features that serve niche use cases belong in user code, not in the package.

---

## Frontend Stack

- **Bootstrap 5.3.3** — Layout, components, dark/light theme (via CDN)
- **Bootstrap Icons 1.11.3** — Icon set (via CDN)
- **HTMX 2.0.4** — Dynamic updates without JS framework (via CDN)
- **Tom Select 2.4.3** — Enhanced select inputs with search (via CDN)
- **Marked + DOMPurify** — AI chat markdown rendering and sanitization (via CDN)
- **Custom CSS** — `style.css` (theme vars, layout), `ai-chat.css` (chat widget)
- **Custom JS** — `app.js` (HTMX handlers, theme toggle, Tom Select init, sidebar), `ai-chat.js` (chat widget)

All frontend dependencies are loaded via CDN. No npm, no build step, no bundler.

---

## Development Conventions

- Version is in `pyproject.toml` under `[project] version`
- Use `hatchling` for builds
- Source layout: all code under `src/fasthx_admin/`
- Examples live in `examples/demo/`
- Commit messages: `feat:`, `fix:`, `refactor:`, `docs:` prefixes
- No test framework currently in use — test via the demo app
