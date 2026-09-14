"""Master kanban leadership view — read fan-out and reserved write spine."""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query, WebSocket, WebSocketDisconnect, status as http_status

from hermes_cli import kanban_db
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_diagnostics as kd
from hermes_cli import kanban_serialize as kser
from hermes_cli.web_read_coalescing import coalesced_read

import importlib.util
import sys
from pathlib import Path

log = logging.getLogger(__name__)

_DASHBOARD_DIR = Path(__file__).resolve().parent


def _load_sibling(stem: str):
    mod_name = f"hermes_kanban_dashboard_{stem}"
    if mod_name in sys.modules:
        return sys.modules[mod_name]
    path = _DASHBOARD_DIR / f"{stem}.py"
    spec = importlib.util.spec_from_file_location(mod_name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


mc = _load_sibling("master_capability")
_registered_router_ids: set[int] = set()


def _ws_upgrade_authorized(ws: WebSocket) -> bool:
    try:
        from hermes_cli import web_server_chat as _ws
    except Exception:
        return True
    return bool(_ws._ws_auth_ok(ws))

# Default Master columns (leadership window — not the full board lane set).
MASTER_DEFAULT_COLUMNS: list[str] = ["ready", "running", "blocked", "review"]
MASTER_OPTIONAL_COLUMNS: list[str] = ["todo", "triage"]

# Empty redirect index reserved for a future cross-board routing table.
REDIRECT_INDEX_SCHEMA: dict[str, Any] = {"redirects": []}


def _placeholders(ids: list) -> str:
    return ",".join(["?"] * len(ids))


def _compute_task_diagnostics(conn: sqlite3.Connection, task_ids: list[str]) -> dict[str, list[dict]]:
    from hermes_cli.config import load_config

    if not task_ids:
        return {}
    diag_config = kd.config_from_runtime_config(load_config())
    rows = conn.execute(f"SELECT * FROM tasks WHERE id IN ({_placeholders(task_ids)})", tuple(task_ids)).fetchall()
    if not rows:
        return {}

    def _rows_by_task(table: str) -> dict[str, list]:
        by_task: dict[str, list] = {tid: [] for tid in task_ids}
        for row in conn.execute(
            f"SELECT * FROM {table} WHERE task_id IN ({_placeholders(task_ids)}) ORDER BY id", tuple(task_ids)
        ):
            by_task.setdefault(row["task_id"], []).append(row)
        return by_task

    events_by_task = _rows_by_task("task_events")
    runs_by_task = _rows_by_task("task_runs")
    graph_by_task = kanban_db.task_graph_contexts(conn, task_ids)
    out: dict[str, list[dict]] = {}
    for r in rows:
        tid = r["id"]
        diags = kd.compute_task_diagnostics(
            r, events_by_task[tid], runs_by_task[tid], config=diag_config, graph=graph_by_task.get(tid)
        )
        if diags:
            out[tid] = [d.to_dict() for d in diags]
    return out


def _warnings_summary_from_diagnostics(diagnostics: list[dict]) -> Optional[dict]:
    if not diagnostics:
        return None
    kinds: dict[str, int] = {}
    count = latest = 0
    highest_idx, highest_sev = -1, None
    for d in diagnostics:
        n = d.get("count", 1)
        kinds[d["kind"]] = kinds.get(d["kind"], 0) + n
        count += n
        latest = max(latest, d.get("last_seen_at") or 0)
        sev = d.get("severity")
        if sev in kd.SEVERITY_ORDER and kd.SEVERITY_ORDER.index(sev) > highest_idx:
            highest_idx, highest_sev = kd.SEVERITY_ORDER.index(sev), sev
    return {"count": count, "kinds": kinds, "latest_at": latest, "highest_severity": highest_sev}


def _attach_diagnostics(task_d: dict, diags: Optional[list[dict]]) -> None:
    if diags:
        task_d["diagnostics"] = diags
        task_d["warnings"] = _warnings_summary_from_diagnostics(diags)


def _active_columns(*, include_todo_triage: bool) -> list[str]:
    cols = list(MASTER_DEFAULT_COLUMNS)
    if include_todo_triage:
        cols = MASTER_OPTIONAL_COLUMNS + cols
    return cols


def _read_board_tasks(slug: str, *, include_archived: bool, tenant: Optional[str]) -> tuple[list[dict], Optional[str]]:
    """Return (serialized_tasks, error_message). One bad DB must not raise."""
    try:
        if not kanban_db.board_exists(slug):
            return [], f"board {slug!r} does not exist"
        kanban_db.init_db(board=slug)
        meta = kanban_db.read_board_metadata(slug)
        with closing(kbc.connect(board=slug)) as conn:
            tasks = kanban_db.list_tasks(conn, tenant=tenant, include_archived=include_archived)
            summary_map = kanban_db.latest_summaries(conn, [t.id for t in tasks])
            task_ids = [t.id for t in tasks]
            diagnostics_per_task = _compute_task_diagnostics(conn, task_ids)
            link_counts: dict[str, dict[str, int]] = {}
            for row in conn.execute("SELECT parent_id, child_id FROM task_links").fetchall():
                link_counts.setdefault(row["parent_id"], {"parents": 0, "children": 0})["children"] += 1
                link_counts.setdefault(row["child_id"], {"parents": 0, "children": 0})["parents"] += 1
            comment_counts: dict[str, int] = {
                r["task_id"]: r["n"]
                for r in conn.execute("SELECT task_id, COUNT(*) AS n FROM task_comments GROUP BY task_id")
            }
            progress: dict[str, dict[str, int]] = {}
            for row in conn.execute(
                "SELECT l.parent_id AS pid, t.status AS cstatus FROM task_links l JOIN tasks t ON t.id = l.child_id"
            ).fetchall():
                p = progress.setdefault(row["pid"], {"done": 0, "total": 0})
                p["total"] += 1
                p["done"] += row["cstatus"] == "done"
            out: list[dict] = []
            for task in tasks:
                full = summary_map.get(task.id)
                extras = {
                    "link_counts": link_counts.get(task.id, {"parents": 0, "children": 0}),
                    "comment_count": comment_counts.get(task.id, 0),
                    "progress": progress.get(task.id),
                }
                d = kser.serialize_task(
                    slug,
                    task,
                    board_meta=meta,
                    latest_summary=full,
                    extras=extras,
                )
                _attach_diagnostics(d, diagnostics_per_task.get(task.id))
                out.append(d)
            return out, None
    except Exception as exc:
        log.warning("Master read failed for board %s: %s", slug, exc)
        return [], str(exc)


def _master_tasks_payload(
    *,
    include_archived: bool = False,
    include_todo_triage: bool = False,
    tenant: Optional[str] = None,
    boards: Optional[list[str]] = None,
) -> dict[str, Any]:
    mc.require_master_capability("read")
    warnings = mc.collapsing_env_warnings()
    active_cols = _active_columns(include_todo_triage=include_todo_triage)
    columns: dict[str, list[dict]] = {c: [] for c in active_cols}
    if include_archived:
        columns["archived"] = []
    errors: list[dict[str, str]] = []
    all_tasks: list[dict] = []
    board_list = kanban_db.list_boards(include_archived=False)
    slugs = [b["slug"] for b in board_list]
    if boards:
        allow = {kanban_db._normalize_board_slug(s) for s in boards if s}
        slugs = [s for s in slugs if s in allow]
    for slug in slugs:
        tasks, err = _read_board_tasks(slug, include_archived=include_archived, tenant=tenant)
        if err:
            errors.append({"board_slug": slug, "error": err})
            continue
        for d in tasks:
            status = d.get("status") or "todo"
            bucket = status if status in columns else ("archived" if include_archived and status == "archived" else "todo")
            if bucket in columns:
                columns[bucket].append(d)
            all_tasks.append(d)
    return {
        "capability": mc.get_master_capability(),
        "warnings": warnings,
        "errors": errors,
        "boards": slugs,
        "tasks": all_tasks,
        "columns": [{"name": name, "tasks": columns[name]} for name in columns],
        "now": int(time.time()),
    }


_read_master_tasks = coalesced_read(_master_tasks_payload)


def register(api_router: APIRouter) -> None:
    """Attach Master routes to the kanban plugin router."""
    router_id = id(api_router)
    if router_id in _registered_router_ids:
        return
    _registered_router_ids.add(router_id)

    @api_router.get("/master/capability")
    def master_capability_endpoint():
        return {
            "capability": mc.get_master_capability(),
            "allowed": list(mc.MASTER_CAPABILITIES),
            "reserved_event_kinds": list(mc.RESERVED_EVENT_KINDS),
            "workspace_policies": list(mc.WORKSPACE_POLICIES),
            "warnings": mc.collapsing_env_warnings(),
        }

    @api_router.get("/master/redirect-index")
    def master_redirect_index():
        mc.require_master_capability("read")
        return REDIRECT_INDEX_SCHEMA

    @api_router.get("/master/tasks")
    async def master_list_tasks(
        include_archived: bool = Query(False),
        include_todo_triage: bool = Query(False, description="Include todo and triage columns"),
        tenant: Optional[str] = Query(None),
        board: Optional[list[str]] = Query(None, description="Restrict to these board slugs"),
    ):
        return await _read_master_tasks(
            include_archived=include_archived,
            include_todo_triage=include_todo_triage,
            tenant=tenant,
            boards=board,
        )

    @api_router.get("/master/tasks/{board_slug}/{task_id}")
    def master_get_task(board_slug: str, task_id: str):
        mc.require_master_capability("read")
        slug = board_slug.strip()
        try:
            slug = kanban_db._normalize_board_slug(slug) or slug
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        if not kanban_db.board_exists(slug):
            raise HTTPException(status_code=404, detail=f"board {slug!r} does not exist")
        tasks, err = _read_board_tasks(slug, include_archived=True, tenant=None)
        if err:
            raise HTTPException(status_code=500, detail=err)
        match = next((t for t in tasks if t.get("id") == task_id), None)
        if match is None:
            raise HTTPException(status_code=404, detail=f"task {task_id} not found on board {slug}")
        return {"task": match, "address": kser.task_address(slug, task_id), "warnings": mc.collapsing_env_warnings()}

    # --- Reserved mutator spine (capability-gated; same handlers later) ------

    @api_router.post("/master/tasks")
    def master_create_task_stub():
        mc.require_master_capability("full-edit")
        mc.reject_if_collapsing_env()
        raise HTTPException(status_code=501, detail="master task create not implemented in read slice")

    @api_router.patch("/master/tasks/{board_slug}/{task_id}")
    def master_patch_task_stub(board_slug: str, task_id: str):
        mc.require_master_capability("move")
        mc.reject_if_collapsing_env()
        raise HTTPException(status_code=501, detail="master task move not implemented in read slice")

    # --- WebSocket: one Master channel, events tagged with board_slug --------

    _EVENT_POLL_SECONDS = 0.3

    class _MasterEventTail:
        def __init__(self, slugs: list[str]) -> None:
            self._slugs = slugs
            self._conns: dict[str, sqlite3.Connection] = {}
            self._executor: Optional[ThreadPoolExecutor] = None

        def _fetch(self, cursors: dict[str, int]) -> tuple[dict[str, int], list[dict]]:
            events: list[dict] = []
            for slug in self._slugs:
                cursor = cursors.get(slug, 0)
                try:
                    if slug not in self._conns:
                        self._conns[slug] = kbc.connect(board=slug)
                    conn = self._conns[slug]
                    rows = conn.execute(
                        "SELECT id, task_id, run_id, kind, payload, created_at "
                        "FROM task_events WHERE id > ? ORDER BY id ASC LIMIT 100",
                        (cursor,),
                    ).fetchall()
                    for r in rows:
                        try:
                            payload = json.loads(r["payload"]) if r["payload"] else None
                        except Exception:
                            payload = None
                        events.append({**dict(r), "board_slug": slug, "payload": payload})
                    if rows:
                        cursors[slug] = rows[-1]["id"]
                except Exception as exc:
                    log.debug("Master WS tail skip board %s: %s", slug, exc)
            events.sort(key=lambda e: (e.get("created_at") or 0, e.get("id") or 0))
            return cursors, events

        def _close(self) -> None:
            for conn in self._conns.values():
                try:
                    conn.close()
                except Exception:
                    pass
            self._conns.clear()

        async def poll(self, cursors: dict[str, int]) -> tuple[dict[str, int], list[dict]]:
            if self._executor is None:
                self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="kanban-master-events")
            return await asyncio.get_running_loop().run_in_executor(self._executor, self._fetch, cursors)

        async def shutdown(self) -> None:
            if self._executor is None:
                return
            try:
                await asyncio.get_running_loop().run_in_executor(self._executor, self._close)
            except Exception as exc:
                log.warning("Master event stream cleanup failed: %s", exc)
            finally:
                self._executor.shutdown(wait=True, cancel_futures=True)

    @api_router.websocket("/master/events")
    async def master_stream_events(ws: WebSocket):
        if not _ws_upgrade_authorized(ws):
            await ws.close(code=http_status.WS_1008_POLICY_VIOLATION)
            return
        await ws.accept()
        slugs = [b["slug"] for b in kanban_db.list_boards(include_archived=False)]
        tail = _MasterEventTail(slugs)
        cursors: dict[str, int] = {}
        for key in ("since",):
            raw = ws.query_params.get(key, "")
            if raw.isdigit():
                # Broadcast cursor applies to every board on first connect.
                val = int(raw)
                for slug in slugs:
                    cursors[slug] = val
        try:
            while True:
                try:
                    msg = await asyncio.wait_for(ws.receive(), timeout=_EVENT_POLL_SECONDS)
                    if msg["type"] == "websocket.disconnect":
                        return
                except asyncio.TimeoutError:
                    pass
                cursors, events = await tail.poll(cursors)
                if events:
                    await ws.send_json({"events": events, "cursors": cursors})
        except WebSocketDisconnect:
            return
        except asyncio.CancelledError:
            return
        except Exception as exc:
            log.warning("Master event stream error: %s", exc)
            try:
                await ws.close()
            except Exception:
                pass
        finally:
            await tail.shutdown()
