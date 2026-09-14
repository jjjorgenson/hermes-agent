"""Master kanban read slice — serialize_task, capability 403, partial GET, composite keys.

Jason review style: each contract prints expected vs actual, not bare PASSED.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_serialize as kser


def _log_check(label: str, expected: Any, actual: Any) -> None:
    print(f"\n[{label}]")
    print(f"  expected: {json.dumps(expected, default=str) if not isinstance(expected, str) else expected!r}")
    print(f"  actual:   {json.dumps(actual, default=str) if not isinstance(actual, str) else actual!r}")


def _load_plugin_router():
    repo_root = Path(__file__).resolve().parents[2]
    plugin_file = repo_root / "plugins" / "kanban" / "dashboard" / "plugin_api.py"
    spec = importlib.util.spec_from_file_location("hermes_dashboard_plugin_kanban_master_test", plugin_file)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod.router


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


@pytest.fixture
def client(kanban_home):
    app = FastAPI()
    app.include_router(_load_plugin_router(), prefix="/api/plugins/kanban")
    return TestClient(app)


def test_serialize_task_includes_board_identity(kanban_home):
    kb.create_board("alpha", name="Alpha Fleet", icon="rocket", color="#ff00aa")
    conn = kbc.connect(board="alpha")
    task_id = kb.create_task(conn, title="Ship it", assignee="ops", created_by="test", board="alpha")
    task = kb.get_task(conn, task_id)
    assert task is not None
    payload = kser.serialize_task("alpha", task, latest_summary="summary line")

    checks = {
        "board_slug": "alpha",
        "board_name": "Alpha Fleet",
        "board_icon": "rocket",
        "board_color": "#ff00aa",
        "title": "Ship it",
        "latest_summary": "summary line",
    }
    for key, expected in checks.items():
        actual = payload.get(key)
        _log_check(f"serialize_task.{key}", expected, actual)
        assert actual == expected

    _log_check("serialize_task.write_safe_root absent", "key missing", "present" if "write_safe_root" in payload else "key missing")
    assert "write_safe_root" not in payload


def test_task_address_composite_key():
    expected = {"board_slug": "my-board", "task_id": "t_abc123"}
    actual = kser.task_address("my-board", "t_abc123")
    _log_check("task_address", expected, actual)
    assert actual == expected


def test_master_tasks_fan_out_partial_success(client, kanban_home):
    kb.create_board("good")
    with kbc.connect(board="good") as conn:
        kb.create_task(conn, title="On good board", created_by="test", board="good")

    kb.create_board("bad")
    bad_db = kb.kanban_db_path(board="bad")
    bad_db.write_text("not sqlite", encoding="utf-8")

    response = client.get("/api/plugins/kanban/master/tasks")
    data = response.json()

    _log_check("GET /master/tasks status", 200, response.status_code)
    assert response.status_code == 200, response.text

    _log_check("capability default", "read", data.get("capability"))
    assert data["capability"] == "read"

    good_slugs = [t["board_slug"] for t in data["tasks"]]
    _log_check("good board present in tasks", "good", good_slugs)
    assert "good" in good_slugs

    error_slugs = [e["board_slug"] for e in data["errors"]]
    _log_check("bad board in errors[]", "bad", error_slugs)
    assert "bad" in error_slugs

    all_tagged = all("board_slug" in t for t in data["tasks"])
    _log_check("every task carries board_slug", True, all_tagged)
    assert all_tagged


def test_master_get_one_uses_composite_address(client, kanban_home):
    kb.create_board("target")
    with kbc.connect(board="target") as conn:
        task_id = kb.create_task(conn, title="Find me", created_by="test", board="target")

    ok = client.get(f"/api/plugins/kanban/master/tasks/target/{task_id}")
    body = ok.json()
    expected_addr = {"board_slug": "target", "task_id": task_id}

    _log_check("GET one status", 200, ok.status_code)
    assert ok.status_code == 200, ok.text

    _log_check("composite address", expected_addr, body.get("address"))
    assert body["address"] == expected_addr

    _log_check("task.board_slug", "target", body["task"].get("board_slug"))
    assert body["task"]["board_slug"] == "target"

    missing = client.get("/api/plugins/kanban/master/tasks/target/t_missing")
    _log_check("missing task status", 404, missing.status_code)
    assert missing.status_code == 404


def test_master_capability_403_body(client, kanban_home, monkeypatch):
    import hermes_kanban_dashboard_master_capability as mc_mod

    monkeypatch.setattr(mc_mod, "get_master_capability", lambda: "read")
    response = client.patch("/api/plugins/kanban/master/tasks/default/t_x")
    detail = response.json()["detail"]
    expected = {"error": "master_capability", "required": "move", "current": "read"}

    _log_check("PATCH /master/tasks status", 403, response.status_code)
    assert response.status_code == 403, response.text

    _log_check("403 body", expected, detail)
    assert detail == expected


def test_master_create_requires_full_edit(client, kanban_home, monkeypatch):
    import hermes_kanban_dashboard_master_capability as mc_mod

    monkeypatch.setattr(mc_mod, "get_master_capability", lambda: "move")
    response = client.post("/api/plugins/kanban/master/tasks", json={"title": "nope"})
    detail = response.json()["detail"]

    _log_check("POST /master/tasks at move capability status", 403, response.status_code)
    assert response.status_code == 403

    _log_check("required capability", "full-edit", detail.get("required"))
    assert detail["required"] == "full-edit"


def test_collapsing_env_warns_on_master_read(client, kanban_home, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(kanban_home / "kanban.db"))
    response = client.get("/api/plugins/kanban/master/tasks")
    warnings = response.json().get("warnings", [])
    has_pin = any("HERMES_KANBAN_DB" in w for w in warnings)

    _log_check("GET /master/tasks with global DB pin status", 200, response.status_code)
    assert response.status_code == 200

    _log_check("warnings mention HERMES_KANBAN_DB", True, has_pin)
    assert has_pin


def test_collapsing_env_does_not_shadow_read_403(client, kanban_home, monkeypatch):
    """Capability 403 must win over env-collapse 409 at read."""
    import hermes_kanban_dashboard_master_capability as mc_mod

    monkeypatch.setenv("HERMES_KANBAN_DB", str(kanban_home / "kanban.db"))
    monkeypatch.setattr(mc_mod, "get_master_capability", lambda: "read")
    response = client.patch("/api/plugins/kanban/master/tasks/default/t_x")
    detail = response.json()["detail"]
    expected = {"error": "master_capability", "required": "move", "current": "read"}

    _log_check("PATCH at read + collapsing env status", 403, response.status_code)
    assert response.status_code == 403
    _log_check("403 body not shadowed by 409", expected, detail)
    assert detail == expected


def test_collapsing_env_409_on_master_mutator(client, kanban_home, monkeypatch):
    import hermes_kanban_dashboard_master_capability as mc_mod

    monkeypatch.setenv("HERMES_KANBAN_DB", str(kanban_home / "kanban.db"))
    monkeypatch.setattr(mc_mod, "get_master_capability", lambda: "full-edit")
    response = client.post("/api/plugins/kanban/master/tasks", json={"title": "blocked"})
    detail = response.json()["detail"]

    _log_check("POST /master/tasks under env collapse status", 409, response.status_code)
    assert response.status_code == 409

    _log_check("409 error kind", "kanban_env_collapse", detail.get("error"))
    assert detail["error"] == "kanban_env_collapse"
