"""
Reusable CRUD view generator that introspects SQLAlchemy models
to auto-generate FastAPI routes + Jinja2 templates.

This replaces Flask-Admin's ModelView with full control over rendering.
"""

from __future__ import annotations

import csv
import functools
import io
import json
import math
from collections import defaultdict
from inspect import Parameter, signature
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from fastapi import APIRouter, FastAPI, Request, Depends
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import inspect, or_, String, cast
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .auth import get_current_user
from .database import get_db

_PACKAGE_DIR = Path(__file__).resolve().parent

# Maps SQLAlchemy column types to HTML input types
COLUMN_TYPE_MAP = {
    "Integer": "number",
    "String": "text",
    "VARCHAR": "text",
    "Text": "textarea",
    "Boolean": "checkbox",
    "Float": "number",
    "DateTime": "datetime-local",
    "Date": "date",
    "Enum": "select",
}

# Maps SQLAlchemy column type names to available filter operations.
# Each operation is (key, label) where key is used in the URL.
FILTER_OPS_STRING = [
    ("contains", "contains"),
    ("not_contains", "not contains"),
    ("equals", "equals"),
    ("not_equal", "not equal"),
    ("empty", "empty"),
    ("in_list", "in list"),
]
FILTER_OPS_NUMERIC = [
    ("equals", "equals"),
    ("not_equal", "not equal"),
    ("greater", "greater than"),
    ("smaller", "smaller than"),
    ("empty", "empty"),
]
FILTER_OPS_BOOLEAN = [
    ("equals", "equals"),
    ("not_equal", "not equal"),
]
FILTER_OPS_DATE = [
    ("equals", "equals"),
    ("not_equal", "not equal"),
    ("greater", "greater than"),
    ("smaller", "smaller than"),
    ("empty", "empty"),
]

# Map SQLAlchemy type names to filter operation lists
FILTER_TYPE_OPS = {
    "String": FILTER_OPS_STRING,
    "VARCHAR": FILTER_OPS_STRING,
    "Text": FILTER_OPS_STRING,
    "Integer": FILTER_OPS_NUMERIC,
    "Float": FILTER_OPS_NUMERIC,
    "Boolean": FILTER_OPS_BOOLEAN,
    "DateTime": FILTER_OPS_DATE,
    "Date": FILTER_OPS_DATE,
    "Enum": FILTER_OPS_STRING,
}

# Global registry of model classes by table name, populated during CRUDView init
_model_registry: Dict[str, Any] = {}


class ValidationError(Exception):
    """Raised from ``CRUDView.validate`` or ``_apply_form_data`` to abort a create/edit.

    Usage::

        class MyView(CRUDView):
            model = MyModel

            def validate(self, item, form_data, is_new):
                if not form_data.get("hostname"):
                    raise ValidationError("Hostname is required")
                if not form_data.get("hostname").endswith(".local"):
                    raise ValidationError("Hostname must end with .local")
    """

    def __init__(self, message: str):
        self.message = message
        super().__init__(message)


def toast_response(
    message: str,
    type: str = "info",
    title: str | None = None,
    redirect: str | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    """Return an HTMLResponse that triggers a toast notification via HTMX.

    Usage in a custom endpoint::

        @CRUDView.endpoint("/{name}/{item_id}/deploy", methods=["POST"])
        async def deploy(self, ...):
            ...
            return toast_response("Deployment started!", type="success", redirect=f"/{self.name}")

    Args:
        message: The toast message text.
        type: One of "success", "danger", "warning", "info".
        title: Optional title (defaults to capitalised type).
        redirect: Optional URL — adds HX-Redirect header for page navigation after toast.
        status_code: HTTP status code (default 200).
    """
    import urllib.parse
    toast_data: Dict[str, Any] = {"message": message, "type": type}
    if title:
        toast_data["title"] = title
    headers: Dict[str, str] = {}
    if redirect:
        # When redirecting, pass the toast as a cookie so it survives the
        # full page navigation triggered by HX-Redirect.
        headers["HX-Redirect"] = redirect
    else:
        headers["HX-Trigger"] = json.dumps({"showToast": toast_data})
    response = HTMLResponse("", status_code=status_code, headers=headers)
    if redirect:
        response.set_cookie(
            "_toast",
            urllib.parse.quote(json.dumps(toast_data)),
            max_age=10,
            httponly=False,
            samesite="lax",
        )
    return response


def _parse_filter_params(request: Request, column_filters, column_labels=None) -> list:
    """Parse filter parameters from query string.

    URL format: ``flt{index}_{column}_{operation}={value}``

    Returns a list of ``(column, operation, value, label)`` tuples where
    *label* is a human-readable column name derived from the key.
    """
    if not column_filters:
        return []
    allowed = set(column_filters)
    _labels = column_labels or {}
    indexed: list[tuple] = []
    for key, value in request.query_params.items():
        if not key.startswith("flt"):
            continue
        rest = key[3:]
        parts = rest.split("_", 1)
        if len(parts) < 2:
            continue
        if parts[0].isdigit():
            idx = int(parts[0])
            col_op = parts[1]
        else:
            idx = 0
            col_op = rest.lstrip("_")
        matched_col = None
        matched_op = None
        for col_name in sorted(allowed, key=len, reverse=True):
            if col_op.startswith(col_name + "_"):
                matched_col = col_name
                matched_op = col_op[len(col_name) + 1:]
                break
        if matched_col and matched_op:
            label = _labels.get(matched_col, matched_col.replace("_", " ").title())
            indexed.append((idx, matched_col, matched_op, value, label))
    indexed.sort(key=lambda x: x[0])
    return [(col, op, val, lbl) for _, col, op, val, lbl in indexed]


def _build_filter_defs(view, model, db: Session = None) -> list:
    """Build filter definitions for the template.

    Returns a list of dicts with keys: col, label, ops, choices (optional).
    When *choices* is present, the template should render a ``<select>``
    for the value field instead of a text ``<input>``.
    """
    if not view.column_filters:
        return []
    mapper = inspect(model)
    col_map = {c.key: c for c in mapper.columns}
    defs = []
    for col_key in view.column_filters:
        col_obj = col_map.get(col_key)
        if col_obj is None:
            continue
        col_type = type(col_obj.type).__name__
        label = view.column_labels.get(col_key, col_key.replace("_", " ").title()) if view.column_labels else col_key.replace("_", " ").title()

        # For FK columns: provide choices from the related table and simpler ops
        if col_key in view.foreign_keys and db is not None:
            fk_options = view._get_fk_options(db, col_key)
            choices = [[str(pk), display] for pk, display in fk_options]
            ops = [["equals", "equals"], ["not_equal", "not equal"], ["empty", "empty"]]
            defs.append({"col": col_key, "label": label, "ops": ops, "fk_choices": choices})
        else:
            ops = FILTER_TYPE_OPS.get(col_type, FILTER_OPS_STRING)
            defs.append({"col": col_key, "label": label, "ops": [list(o) for o in ops]})
    return defs


def _safe_build_query(view, db, q, sort, order, active_filters):
    """Call ``view._build_query`` with filters, falling back if the subclass
    overrides ``_build_query`` without a *filters* parameter."""
    try:
        return view._build_query(db, search=q, sort=sort, order=order, filters=active_filters)
    except TypeError:
        # Subclass override doesn't accept 'filters' — call without it
        query = view._build_query(db, search=q, sort=sort, order=order)
        # Apply filters manually on the returned query
        if active_filters:
            for col_key, op, value, *_ in active_filters:
                col = getattr(view.model, col_key, None)
                if col is None:
                    continue
                mapper = inspect(view.model)
                ctype = {c.key: type(c.type).__name__ for c in mapper.columns}.get(col_key, "")
                if ctype in ("Integer",) and op != "empty":
                    try:
                        value = int(value)
                    except (ValueError, TypeError):
                        continue
                if op == "equals":
                    query = query.filter(col == value)
                elif op == "not_equal":
                    query = query.filter(col != value)
                elif op == "contains":
                    query = query.filter(cast(col, String).ilike(f"%{value}%"))
                elif op == "not_contains":
                    query = query.filter(~cast(col, String).ilike(f"%{value}%"))
                elif op == "empty":
                    query = query.filter((col == None) | (cast(col, String) == ""))  # noqa: E711
                elif op == "greater":
                    query = query.filter(col > value)
                elif op == "smaller":
                    query = query.filter(col < value)
        return query


class CRUDView:
    """
    Given a SQLAlchemy model, generates list/detail/create/edit/delete routes.

    Subclass this and set class-level attributes to configure the view::

        class CustomerView(CRUDView):
            model = Customer
            column_list = ["id", "name", "sid"]
            form_sections = {"Basic": ["name", "sid"]}

    Then register via Admin::

        admin = Admin(app, templates)
        admin.add_view(CustomerView)
    """

    # --- Endpoint decorator ---

    @staticmethod
    def endpoint(path: str, methods: list[str] | None = None, **route_kwargs):
        """Decorator for declaring custom endpoints on a CRUDView subclass.

        Usage::

            class MyView(CRUDView):
                model = MyModel

                @CRUDView.endpoint("/{name}/{item_id}/reset", methods=["POST"])
                async def reset(self, request: Request, item_id: int, db: Session = Depends(get_db)):
                    ...

        ``{name}`` in the path is replaced with ``self.name`` at init time.
        The ``self`` parameter is bound automatically and hidden from FastAPI.
        """
        if methods is None:
            methods = ["GET"]

        def decorator(fn):
            fn._endpoint_meta = {
                "path": path,
                "methods": methods,
                "route_kwargs": route_kwargs,
            }
            return fn

        return decorator

    # --- Class-level config (override in subclasses) ---
    model = None
    name = None
    display_name = None
    category = None
    icon = None
    column_list = None
    column_exclude = None
    column_labels = None
    column_formatters = None
    column_searchable = None
    column_sortable = None
    form_columns = None
    form_sections = None
    form_widget_overrides = None
    form_ajax_refs = None
    row_actions = None
    page_size = 20
    pk_field = "id"
    can_create = True
    can_edit = True
    can_delete = True
    htmx_columns = None
    column_filters = None
    export_types = None
    list_template = "list.html"
    create_template = "form.html"
    edit_template = "form.html"

    def __init__(self, templates):
        model = self.model
        if model is None:
            raise ValueError(f"{type(self).__name__} must define a 'model' attribute")

        self.templates = templates

        # Resolve defaults from model metadata where not set on the class
        if self.name is None:
            self.name = model.__tablename__
        if self.display_name is None:
            self.display_name = getattr(model, "__admin_name__", self.name.replace("_", " ").title())
        if self.category is None:
            self.category = getattr(model, "__admin_category__", None)
        if self.icon is None:
            self.icon = getattr(model, "__admin_icon__", "table")

        # Resolve mutable defaults (None -> empty collection)
        self.column_formatters = self.column_formatters or {}
        self.column_labels = self.column_labels or {}
        self.form_widget_overrides = self.form_widget_overrides or {}
        self.form_ajax_refs = self.form_ajax_refs or {}
        self.row_actions = self.row_actions or []
        self.htmx_columns = self.htmx_columns or {}

        # Register model in our registry
        _model_registry[model.__tablename__] = model

        # Introspect the model
        mapper = inspect(model)
        all_columns = [col.key for col in mapper.columns]
        self.relationships = {
            rel.key: rel for rel in mapper.relationships
        }
        self.foreign_keys = {}
        for col in mapper.columns:
            for fk in col.foreign_keys:
                self.foreign_keys[col.key] = fk
                # Register FK target models that may not have their own CRUDView
                target_table = fk.column.table
                if target_table.name not in _model_registry:
                    for rel in mapper.relationships:
                        rel_mapper = rel.mapper
                        if rel_mapper.local_table.name == target_table.name:
                            _model_registry[target_table.name] = rel_mapper.class_
                            break

        # Determine which columns to show in the list
        if self.column_list:
            pass  # already set on class
        elif self.column_exclude:
            self.column_list = [c for c in all_columns if c not in self.column_exclude]
        else:
            self.column_list = all_columns

        # Determine which columns to show in forms
        if not self.form_columns:
            self.form_columns = [
                c for c in all_columns
                if c != self.pk_field and c != "deploy_progress"
            ]

        # Build column metadata for templates (ordered by column_list)
        col_map_meta = {col_obj.key: col_obj for col_obj in mapper.columns}
        self.columns_meta = []
        for key in self.column_list:
            col_obj = col_map_meta.get(key)
            if col_obj is not None:
                col_type = type(col_obj.type).__name__
                self.columns_meta.append({
                    "key": col_obj.key,
                    "label": self.column_labels.get(col_obj.key, col_obj.key.replace("_", " ").title()),
                    "type": col_type,
                    "sortable": self.column_sortable is None or col_obj.key in (self.column_sortable or []),
                })

        # Build form field metadata (ordered by form_columns)
        self.form_fields = []
        col_map = {col_obj.key: col_obj for col_obj in mapper.columns}
        for col_key in self.form_columns:
            col_obj = col_map.get(col_key)
            if col_obj is not None:
                col_type = type(col_obj.type).__name__
                html_type = COLUMN_TYPE_MAP.get(col_type, "text")

                # Check if this is an enum column
                choices = None
                if hasattr(col_obj.type, "enum_class") and col_obj.type.enum_class:
                    choices = [(e.value, e.value.title()) for e in col_obj.type.enum_class]
                    html_type = "select"

                # Check if this is a foreign key
                if col_obj.key in self.foreign_keys:
                    if self.form_ajax_refs and col_obj.key in self.form_ajax_refs:
                        html_type = "ajax_select"
                    else:
                        html_type = "select"

                field = {
                    "key": col_obj.key,
                    "label": self.column_labels.get(col_obj.key, col_obj.key.replace("_", " ").title()),
                    "type": html_type,
                    "required": not col_obj.nullable and col_obj.default is None,
                    "choices": choices,
                    "is_fk": col_obj.key in self.foreign_keys,
                }
                field.update(self.form_widget_overrides.get(col_obj.key, {}))
                self.form_fields.append(field)

        # Build searchable columns
        if self.column_searchable is None:
            self.column_searchable = [
                col.key for col in mapper.columns
                if isinstance(col.type, String)
            ]

        self.router = APIRouter()
        self._setup_htmx_polling_routes()
        self._setup_ajax_select_routes()
        self._setup_decorated_endpoints()
        self.setup_endpoints()
        self._setup_routes()

    def _setup_ajax_select_routes(self):
        """Register HTMX search endpoints for form_ajax_refs fields."""
        if not self.form_ajax_refs:
            return

        view = self

        for field_key, config in self.form_ajax_refs.items():
            target_model = config["model"]
            search_fields = config.get("fields", [])
            page_size = config.get("page_size", 10)

            def make_handler(fk, tgt_model, s_fields, p_size):
                async def search_handler(
                    request: Request,
                    q: str = "",
                    page: int = 1,
                    db: Session = Depends(get_db),
                ):
                    query = db.query(tgt_model)
                    if q and s_fields:
                        filters = [
                            getattr(tgt_model, f).ilike(f"%{q}%")
                            for f in s_fields
                            if hasattr(tgt_model, f)
                        ]
                        if filters:
                            query = query.filter(or_(*filters))

                    items = query.offset((page - 1) * p_size).limit(p_size).all()

                    results = []
                    for item in items:
                        results.append({
                            "value": str(getattr(item, "id", "")),
                            "label": str(item),
                        })
                    return results

                search_handler.__name__ = f"{view.name}_{fk}_ajax_search"
                return search_handler

            handler = make_handler(field_key, target_model, search_fields, page_size)
            self.router.add_api_route(
                f"/{self.name}/ajax/{field_key}",
                handler,
                methods=["GET"],
            )

    def _get_fk_options(self, db: Session, field_key: str) -> list:
        """Get options for a foreign key select field."""
        fk = self.foreign_keys.get(field_key)
        if not fk:
            return []
        target_table = fk.column.table
        target_model = _model_registry.get(target_table.name)
        if target_model:
            items = db.query(target_model).all()
            return [(getattr(item, 'id', str(item)), str(item)) for item in items]
        return []

    def _build_query(self, db: Session, search: str = "", sort: str = "", order: str = "asc", filters: list | None = None):
        """Build a query with search, sorting, and column filters.

        ``filters`` is a list of (col_key, operation, value) tuples.
        """
        query = db.query(self.model)

        # Apply column filters
        if filters:
            mapper = inspect(self.model)
            col_type_map = {c.key: type(c.type).__name__ for c in mapper.columns}
            for col_key, op, value, *_ in filters:
                col = getattr(self.model, col_key, None)
                if col is None:
                    continue
                # Cast value for numeric columns
                ctype = col_type_map.get(col_key, "")
                if ctype in ("Integer",) and op != "empty":
                    try:
                        value = int(value)
                    except (ValueError, TypeError):
                        continue  # skip invalid numeric filter
                elif ctype in ("Float",) and op != "empty":
                    try:
                        value = float(value)
                    except (ValueError, TypeError):
                        continue
                if op == "contains":
                    query = query.filter(cast(col, String).ilike(f"%{value}%"))
                elif op == "not_contains":
                    query = query.filter(~cast(col, String).ilike(f"%{value}%"))
                elif op == "equals":
                    query = query.filter(col == value)
                elif op == "not_equal":
                    query = query.filter(col != value)
                elif op == "empty":
                    query = query.filter((col == None) | (cast(col, String) == ""))  # noqa: E711
                elif op == "greater":
                    query = query.filter(col > value)
                elif op == "smaller":
                    query = query.filter(col < value)
                elif op == "in_list":
                    vals = [v.strip() for v in str(value).split(",") if v.strip()]
                    if vals:
                        query = query.filter(col.in_(vals))

        if search and self.column_searchable:
            mapper = inspect(self.model)
            search_filters = []
            for col_key in self.column_searchable:
                col = mapper.columns[col_key]
                if isinstance(col.type, String):
                    search_filters.append(col.ilike(f"%{search}%"))
                else:
                    search_filters.append(cast(col, String).ilike(f"%{search}%"))
            if search_filters:
                query = query.filter(or_(*search_filters))

        if sort:
            mapper = inspect(self.model)
            if sort in [c.key for c in mapper.columns]:
                col = getattr(self.model, sort)
                query = query.order_by(col.desc() if order == "desc" else col.asc())
        else:
            query = query.order_by(getattr(self.model, self.pk_field).desc())

        return query

    def get_colspan(self) -> int:
        """Calculate table colspan (columns + actions column if present)."""
        return len(self.columns_meta) + (1 if self.row_actions else 0)

    def _setup_htmx_polling_routes(self):
        """Auto-register GET endpoints for each htmx_columns entry."""
        if not self.htmx_columns:
            return

        model = self.model
        templates = self.templates
        view = self
        formatters = self.column_formatters or {}

        for field_key, config in self.htmx_columns.items():
            # Convert URL pattern: /edges/{id}/status -> /edges/{item_id}/status
            url = config["url"].replace("{id}", "{item_id}")
            formatter = formatters.get(field_key)

            def make_handler(fk, fmt):
                async def handler(request: Request, item_id, db: Session = Depends(get_db)):
                    item = db.query(model).filter(getattr(model, view.pk_field) == item_id).first()
                    if not item:
                        return HTMLResponse("")
                    value = getattr(item, fk)
                    if fmt:
                        return HTMLResponse(fmt(value, item))
                    status = value.value if hasattr(value, "value") else str(value)
                    return templates.TemplateResponse("partials/status_cell.html", {
                        "request": request,
                        "status": status,
                    })
                handler.__name__ = f"{view.name}_{fk}_poll"
                return handler

            self.router.add_api_route(
                url,
                make_handler(field_key, formatter),
                methods=["GET"],
                response_class=HTMLResponse,
            )

    def _setup_decorated_endpoints(self):
        """Collect methods decorated with @CRUDView.endpoint and register them."""
        import typing

        for attr_name in dir(type(self)):
            fn = getattr(type(self), attr_name, None)
            if fn is None or not callable(fn) or not hasattr(fn, "_endpoint_meta"):
                continue

            meta = fn._endpoint_meta
            path = meta["path"].replace("{name}", self.name).replace("{prefix}", f"/{self.name}")

            bound = fn.__get__(self, type(self))

            # Resolve string annotations (from `from __future__ import annotations`)
            # back to real types so FastAPI can process Depends(), Request, etc.
            hints = typing.get_type_hints(fn, include_extras=True)

            sig = signature(fn)
            params = []
            for pname, p in sig.parameters.items():
                if pname == "self":
                    continue
                annotation = hints.get(pname, p.annotation)
                params.append(p.replace(annotation=annotation))
            new_sig = sig.replace(
                parameters=params,
                return_annotation=hints.get("return", sig.return_annotation),
            )

            @functools.wraps(fn)
            async def make_handler(bound_method=bound, **kwargs):
                return await bound_method(**kwargs)

            make_handler.__signature__ = new_sig

            self.router.add_api_route(
                path,
                make_handler,
                methods=meta["methods"],
                **meta["route_kwargs"],
            )

    def setup_endpoints(self):
        """Override in subclasses to register custom HTMX endpoints on self.router."""
        pass

    def _setup_routes(self):
        model = self.model
        templates = self.templates
        view = self

        @self.router.get(f"/{self.name}", response_class=HTMLResponse)
        async def list_view(
            request: Request,
            page: int = 1,
            q: str = "",
            sort: str = "",
            order: str = "asc",
            db: Session = Depends(get_db),
        ):
            # Parse active filters from query params (flt{idx}_{col}_{op}=value)
            active_filters = _parse_filter_params(request, view.column_filters, view.column_labels)

            query = _safe_build_query(view, db, q, sort, order, active_filters)
            total = query.count()
            total_pages = max(1, math.ceil(total / view.page_size))
            page = max(1, min(page, total_pages))
            items = query.offset((page - 1) * view.page_size).limit(view.page_size).all()

            rows = []
            for item in items:
                row = {"_obj": item, "_id": getattr(item, view.pk_field), "cells": {}}
                for col_meta in view.columns_meta:
                    key = col_meta["key"]
                    value = getattr(item, key)
                    if key in view.column_formatters:
                        formatted = view.column_formatters[key](value, item)
                    else:
                        formatted = value
                    row["cells"][key] = {
                        "raw": value,
                        "formatted": formatted,
                        "htmx": view.htmx_columns.get(key),
                    }
                rows.append(row)

            # Build filter definitions for the template
            filter_defs = _build_filter_defs(view, model, db)

            context = {
                "request": request,
                "view": view,
                "rows": rows,
                "columns": view.columns_meta,
                "page": page,
                "total_pages": total_pages,
                "total": total,
                "search": q,
                "sort": sort,
                "order": order,
                "row_actions": view.row_actions,
                "filter_defs": filter_defs,
                "active_filters": active_filters,
            }

            if request.headers.get("HX-Request") and request.query_params.get("partial"):
                return templates.TemplateResponse("partials/table_body.html", context)

            return templates.TemplateResponse(view.list_template, context)

        if view.export_types:
            @self.router.get(f"/{self.name}/export/{{fmt}}")
            async def export_view(
                request: Request,
                fmt: str,
                q: str = "",
                sort: str = "",
                order: str = "asc",
                db: Session = Depends(get_db),
            ):
                if fmt not in view.export_types:
                    return HTMLResponse("Export format not supported", status_code=400)

                active_filters = _parse_filter_params(request, view.column_filters, view.column_labels)

                query = _safe_build_query(view, db, q, sort, order, active_filters)
                items = query.all()

                # Get column keys and labels
                col_keys = [c["key"] for c in view.columns_meta]
                col_labels = [c["label"] for c in view.columns_meta]

                # Build rows of raw values
                rows_data = []
                for item in items:
                    row = []
                    for key in col_keys:
                        value = getattr(item, key, "")
                        if hasattr(value, "value"):
                            value = value.value
                        if value is None:
                            value = ""
                        row.append(str(value))
                    rows_data.append(row)

                if fmt == "csv":
                    output = io.StringIO()
                    writer = csv.writer(output)
                    writer.writerow(col_labels)
                    writer.writerows(rows_data)
                    return StreamingResponse(
                        iter([output.getvalue()]),
                        media_type="text/csv",
                        headers={"Content-Disposition": f"attachment; filename={view.name}.csv"},
                    )

                if fmt == "xlsx":
                    try:
                        import openpyxl
                    except ImportError:
                        return HTMLResponse(
                            "openpyxl is required for XLSX export: pip install openpyxl",
                            status_code=500,
                        )
                    wb = openpyxl.Workbook()
                    ws = wb.active
                    ws.title = view.display_name
                    ws.append(col_labels)
                    for row in rows_data:
                        ws.append(row)
                    output = io.BytesIO()
                    wb.save(output)
                    output.seek(0)
                    return StreamingResponse(
                        output,
                        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        headers={"Content-Disposition": f"attachment; filename={view.name}.xlsx"},
                    )

                return HTMLResponse("Unsupported format", status_code=400)

        @self.router.get(f"/{self.name}/create", response_class=HTMLResponse)
        async def create_form(
            request: Request,
            db: Session = Depends(get_db),
        ):
            if not view.can_create:
                return HTMLResponse("Create not allowed", status_code=403)

            form_fields = view._prepare_form_fields(db)

            return templates.TemplateResponse(view.create_template, {
                "request": request,
                "view": view,
                "form_fields": form_fields,
                "form_sections": view.form_sections,
                "item": None,
                "action": f"/{view.name}/create",
                "title": f"Create {view.display_name}",
            })

        @self.router.post(f"/{self.name}/create", response_class=HTMLResponse)
        async def create_submit(
            request: Request,
            db: Session = Depends(get_db),
        ):
            form_data = await request.form()
            item = model()
            try:
                view._apply_form_data(item, form_data)
                view.validate(item, form_data, is_new=True)
                db.add(item)
                db.commit()
            except ValidationError as e:
                db.rollback()
                error_msg = e.message
            except IntegrityError as e:
                db.rollback()
                error_msg = "A required field is missing or a value already exists."
            except Exception as e:
                db.rollback()
                error_msg = str(e) or "An unexpected error occurred."
            else:
                error_msg = None
            if error_msg:
                form_fields = view._prepare_form_fields(db, item)
                return templates.TemplateResponse(view.create_template, {
                    "request": request,
                    "view": view,
                    "form_fields": form_fields,
                    "form_sections": view.form_sections,
                    "item": None,
                    "action": f"/{view.name}/create",
                    "title": f"Create {view.display_name}",
                }, headers={
                    "HX-Trigger": json.dumps({"showToast": {
                        "message": error_msg, "type": "danger", "title": "Validation Error",
                    }}),
                })
            if request.headers.get("HX-Request"):
                return HTMLResponse("", headers={"HX-Redirect": f"/{view.name}"})
            return RedirectResponse(f"/{view.name}", status_code=303)

        @self.router.get(f"/{self.name}/{{item_id}}", response_class=HTMLResponse)
        async def detail_view(
            request: Request,
            item_id,
            db: Session = Depends(get_db),
        ):
            item = db.query(model).filter(getattr(model, view.pk_field) == item_id).first()
            if not item:
                return HTMLResponse("Not found", status_code=404)

            fields = []
            for col_meta in view.columns_meta:
                key = col_meta["key"]
                value = getattr(item, key)
                if key in view.column_formatters:
                    formatted = view.column_formatters[key](value, item)
                else:
                    formatted = value
                fields.append({
                    "label": col_meta["label"],
                    "value": formatted,
                    "raw": value,
                })

            return templates.TemplateResponse("detail.html", {
                "request": request,
                "view": view,
                "item": item,
                "fields": fields,
            })

        @self.router.get(f"/{self.name}/{{item_id}}/edit", response_class=HTMLResponse)
        async def edit_form(
            request: Request,
            item_id,
            db: Session = Depends(get_db),
        ):
            if not view.can_edit:
                return HTMLResponse("Edit not allowed", status_code=403)

            item = db.query(model).filter(getattr(model, view.pk_field) == item_id).first()
            if not item:
                return HTMLResponse("Not found", status_code=404)

            form_fields = view._prepare_form_fields(db, item)

            return templates.TemplateResponse(view.edit_template, {
                "request": request,
                "view": view,
                "form_fields": form_fields,
                "form_sections": view.form_sections,
                "item": item,
                "action": f"/{view.name}/{item_id}/edit",
                "title": f"Edit {view.display_name}",
            })

        @self.router.post(f"/{self.name}/{{item_id}}/edit", response_class=HTMLResponse)
        async def edit_submit(
            request: Request,
            item_id,
            db: Session = Depends(get_db),
        ):
            item = db.query(model).filter(getattr(model, view.pk_field) == item_id).first()
            if not item:
                return HTMLResponse("Not found", status_code=404)

            form_data = await request.form()
            try:
                view._apply_form_data(item, form_data)
                view.validate(item, form_data, is_new=False)
                db.commit()
            except ValidationError as e:
                db.rollback()
                error_msg = e.message
            except IntegrityError as e:
                db.rollback()
                error_msg = "A required field is missing or a value already exists."
            except Exception as e:
                db.rollback()
                error_msg = str(e) or "An unexpected error occurred."
            else:
                error_msg = None
            if error_msg:
                form_fields = view._prepare_form_fields(db, item)
                return templates.TemplateResponse(view.edit_template, {
                    "request": request,
                    "view": view,
                    "form_fields": form_fields,
                    "form_sections": view.form_sections,
                    "item": item,
                    "action": f"/{view.name}/{item_id}/edit",
                    "title": f"Edit {view.display_name}",
                }, headers={
                    "HX-Trigger": json.dumps({"showToast": {
                        "message": error_msg, "type": "danger", "title": "Validation Error",
                    }}),
                })
            if request.headers.get("HX-Request"):
                return HTMLResponse("", headers={"HX-Redirect": f"/{view.name}"})
            return RedirectResponse(f"/{view.name}", status_code=303)

        @self.router.post(f"/{self.name}/{{item_id}}/delete", response_class=HTMLResponse)
        async def delete_item(
            request: Request,
            item_id,
            db: Session = Depends(get_db),
        ):
            if not view.can_delete:
                return HTMLResponse("Delete not allowed", status_code=403)

            item = db.query(model).filter(getattr(model, view.pk_field) == item_id).first()
            if item:
                db.delete(item)
                db.commit()

            if request.headers.get("HX-Request"):
                return HTMLResponse("")
            return RedirectResponse(f"/{view.name}", status_code=303)

    def _prepare_form_fields(self, db: Session, item=None) -> list:
        """Prepare form fields with current values and FK options."""
        fields = []
        for field in self.form_fields:
            f = dict(field)
            if item:
                f["value"] = getattr(item, field["key"])
            else:
                f["value"] = None

            if field["is_fk"]:
                if field["key"] in self.form_ajax_refs:
                    ajax_cfg = self.form_ajax_refs[field["key"]]
                    f["ajax_url"] = f"/{self.name}/ajax/{field['key']}"
                    f["placeholder"] = ajax_cfg.get("placeholder", "Type to search...")
                    if item and f["value"]:
                        fk = self.foreign_keys.get(field["key"])
                        target_model = _model_registry.get(fk.column.table.name)
                        if target_model:
                            related = db.query(target_model).filter(
                                getattr(target_model, "id") == f["value"]
                            ).first()
                            f["value_label"] = str(related) if related else ""
                else:
                    f["choices"] = self._get_fk_options(db, field["key"])

            fields.append(f)
        return fields

    def _apply_form_data(self, item, form_data):
        """Apply form data to a model instance."""
        mapper = inspect(self.model)
        for field in self.form_fields:
            key = field["key"]
            if key in form_data:
                value = form_data[key]
                col = mapper.columns[key]
                col_type = type(col.type).__name__.upper()

                if col_type == "INTEGER":
                    value = int(value) if value else None
                elif col_type == "FLOAT":
                    value = float(value) if value else None
                elif col_type == "BOOLEAN":
                    value = value in ("true", "1", "on", "True")
                elif not value and col.nullable:
                    value = None

                # Reject empty values for non-nullable fields (except booleans)
                if not value and value != 0 and value is not False and not col.nullable and col.default is None:
                    label = field.get("label") or key.replace("_", " ").title()
                    raise ValidationError(f"{label} is required")

                if hasattr(col.type, "enum_class") and col.type.enum_class and value:
                    value = col.type.enum_class(value)

                setattr(item, key, value)

    def validate(self, item, form_data, is_new: bool):
        """Override to add custom validation before create/edit commits.

        Raise ``ValidationError`` to abort the save and show a toast to the user.

        Args:
            item: The model instance (already has form data applied).
            form_data: The raw form data dict.
            is_new: True for create, False for edit.

        Example::

            def validate(self, item, form_data, is_new):
                if not item.hostname:
                    raise ValidationError("Hostname is required")
                if is_new and self.model.query.filter_by(hostname=item.hostname).first():
                    raise ValidationError("Hostname already exists")
        """
        pass

    def register(self, app):
        """Register this view's routes with the FastAPI app."""
        app.include_router(self.router, tags=[self.display_name])

    def get_nav_info(self) -> dict:
        """Return navigation info for the sidebar."""
        return {
            "name": self.name,
            "display_name": self.display_name,
            "category": self.category,
            "icon": self.icon,
            "url": f"/{self.name}",
        }


class Admin:
    """
    Factory that instantiates CRUDView subclasses, registers them with a
    FastAPI app, and sets up the built-in templates and static assets.

    Usage::

        from fasthx_admin import Admin, CRUDView

        app = FastAPI()
        admin = Admin(app)
        admin.add_view(CustomerView)
    """

    def __init__(
        self,
        app: FastAPI,
        templates: Jinja2Templates | None = None,
        *,
        title: str = "Admin",
        static_url: str = "/static/fasthx-admin",
        mount_statics: bool = True,
        public_pages: set[str] | None = None,
        ai_chat: bool = False,
        extra_templates_dirs: list[str] | None = None,
        settings_admin_groups: list[str] | None = None,
        settings_admin_users: list[str] | None = None,
    ):
        self.app = app
        self.title = title
        self.static_url = static_url
        self.public_pages = public_pages if public_pages is not None else {"login.html"}
        self.views: list[CRUDView] = []
        self._view_map: dict[str, CRUDView] = {}
        self._custom_links: list[dict] = []
        self.ai_chat_enabled = ai_chat
        self.settings_admin_groups = settings_admin_groups
        self.settings_admin_users = settings_admin_users

        # Set up Jinja2 templates (use built-in if not provided)
        if templates is not None:
            self.templates = templates
        else:
            builtin_dir = str(_PACKAGE_DIR / "templates")
            self.templates = Jinja2Templates(directory=builtin_dir)

        # Add extra template search directories (app-level custom templates)
        if extra_templates_dirs:
            from jinja2 import FileSystemLoader
            loader = self.templates.env.loader
            existing = loader.searchpath if hasattr(loader, 'searchpath') else [loader.searchpath]
            self.templates.env.loader = FileSystemLoader(list(extra_templates_dirs) + existing)

        # Mount built-in static files
        if mount_statics:
            static_dir = _PACKAGE_DIR / "static"
            app.mount(
                static_url,
                StaticFiles(directory=str(static_dir)),
                name="fasthx-admin-static",
            )

        # Wrap TemplateResponse to inject nav context + auth check
        self._wrap_template_response()

        # Set up AI chat if enabled
        if ai_chat:
            from .ai_chat import create_ai_chat_router, ensure_ai_tables
            ensure_ai_tables()
            router = create_ai_chat_router(self)
            app.include_router(router)
            self.add_link(
                "ai_settings", "/ai/settings", "AI Settings",
                icon="robot", category="Settings",
            )
            self.add_link(
                "ai_context_settings", "/ai/settings/context", "AI Context & Tools",
                icon="puzzle", category="Settings",
            )

    def _wrap_template_response(self):
        """Monkey-patch TemplateResponse to inject nav categories and auth."""
        _original = self.templates.TemplateResponse
        admin = self

        def _patched(name, context, **kwargs):
            request = context.get("request")
            user = get_current_user(request) if request else None

            # Redirect to login if not authenticated (skip for public pages)
            if name not in admin.public_pages and not user:
                return RedirectResponse("/login", status_code=303)

            context.setdefault("current_user", user)
            context.setdefault("nav_categories", admin.get_nav_categories(user))
            context.setdefault("active_page", "")
            context.setdefault("static_url", admin.static_url)
            context.setdefault("admin_title", admin.title)
            if admin.ai_chat_enabled:
                from .ai_chat import is_chat_widget_enabled
                context.setdefault("ai_chat_enabled", is_chat_widget_enabled())
            else:
                context.setdefault("ai_chat_enabled", False)
            return _original(name, context, **kwargs)

        self.templates.TemplateResponse = _patched

    def add_view(self, view_class: type[CRUDView]) -> CRUDView:
        """Instantiate a CRUDView subclass and register its routes."""
        instance = view_class(self.templates)
        self.views.append(instance)
        self._view_map[instance.name] = instance
        instance.register(self.app)
        return instance

    def get_view(self, name: str) -> CRUDView | None:
        """Look up a registered view by name."""
        return self._view_map.get(name)

    def add_link(
        self,
        name: str,
        url: str,
        display_name: str,
        icon: str = "link",
        category: str = "Other",
    ):
        """Add a custom navigation link to the sidebar."""
        self._custom_links.append({
            "name": name,
            "url": url,
            "display_name": display_name,
            "icon": icon,
            "category": category,
        })

    def get_nav_categories(self, user: dict | None = None) -> dict:
        """Build sidebar navigation from all registered views."""
        categories = defaultdict(list)
        for view in self.views:
            cat = view.category or "Other"
            categories[cat].append(view.get_nav_info())
        for link in self._custom_links:
            cat = link.get("category", "Other")
            categories[cat].append({
                "name": link["name"],
                "url": link["url"],
                "display_name": link["display_name"],
                "icon": link["icon"],
            })

        # Hide Settings category unless user is explicitly allowed.
        # If neither settings_admin_groups nor settings_admin_users is
        # configured, Settings is hidden from everyone by default.
        if "Settings" in categories:
            username = (user or {}).get("username", "")
            user_groups = (user or {}).get("groups", [])
            allowed_by_user = self.settings_admin_users and username in self.settings_admin_users
            allowed_by_group = self.settings_admin_groups and any(
                g in self.settings_admin_groups for g in user_groups
            )
            if not (allowed_by_user or allowed_by_group):
                del categories["Settings"]

        return dict(categories)
