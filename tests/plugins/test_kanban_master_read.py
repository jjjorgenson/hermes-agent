"""Master kanban read slice — serialize_task, capability 403, partial GET, composite keys."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_serialize as kser


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
    assert payload["board_slug"] == "alpha"
    assert payload["board_name"] == "Alpha Fleet"
    assert payload["board_icon"] == "rocket"
    assert payload["board_color"] == "#ff00aa"
    assert payload["title"] == "Ship it"
    assert payload["latest_summary"] == "summary line"
    assert "write_safe_root" not in payload


def test_task_address_composite_key():
    addr = kser.task_address("my-board", "t_abc123")
    assert addr == {"board_slug": "my-board", "task_id": "t_abc123"}


def test_master_tasks_fan_out_partial_success(client, kanban_home):
    kb.create_board("good")
    with kbc.connect(board="good") as conn:
        kb.create_task(conn, title="On good board", created_by="test", board="good")

    kb.create_board("bad")
    bad_db = kb.kanban_db_path(board="bad")
    bad_db.write_text("not sqlite", encoding="utf-8")

    response = client.get("/api/plugins/kanban/master/tasks")
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["capability"] == "read"
    assert any(t["board_slug"] == "good" for t in data["tasks"])
    assert any(e["board_slug"] == "bad" for e in data["errors"])
    assert all("board_slug" in t for t in data["tasks"])


def test_master_get_one_uses_composite_address(client, kanban_home):
    kb.create_board("target")
    with kbc.connect(board="target") as conn:
        task_id = kb.create_task(conn, title="Find me", created_by="test", board="target")

    ok = client.get(f"/api/plugins/kanban/master/tasks/target/{task_id}")
    assert ok.status_code == 200, ok.text
    body = ok.json()
    assert body["address"] == {"board_slug": "target", "task_id": task_id}
    assert body["task"]["board_slug"] == "target"

    missing = client.get("/api/plugins/kanban/master/tasks/target/t_missing")
    assert missing.status_code == 404


def test_master_capability_403_body(client, kanban_home, monkeypatch):
    import hermes_kanban_dashboard_master_capability as mc_mod

    monkeypatch.setattr(mc_mod, "get_master_capability", lambda: "read")
    response = client.patch("/api/plugins/kanban/master/tasks/default/t_x")
    assert response.status_code == 403, response.text
    detail = response.json()["detail"]
    assert detail["error"] == "master_capability"
    assert detail["required"] == "move"
    assert detail["current"] == "read"


def test_master_create_requires_full_edit(client, kanban_home, monkeypatch):
    import hermes_kanban_dashboard_master_capability as mc_mod

    monkeypatch.setattr(mc_mod, "get_master_capability", lambda: "move")
    response = client.post("/api/plugins/kanban/master/tasks", json={"title": "nope"})
    assert response.status_code == 403
    assert response.json()["detail"]["required"] == "full-edit"


def test_collapsing_env_warns_on_master_read(client, kanban_home, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(kanban_home / "kanban.db"))
    response = client.get("/api/plugins/kanban/master/tasks")
    assert response.status_code == 200
    assert any("HERMES_KANBAN_DB" in w for w in response.json()["warnings"])


def test_collapsing_env_409_on_master_mutator(client, kanban_home, monkeypatch):
    import hermes_kanban_dashboard_master_capability as mc_mod

    monkeypatch.setenv("HERMES_KANBAN_DB", str(kanban_home / "kanban.db"))
    monkeypatch.setattr(mc_mod, "get_master_capability", lambda: "full-edit")
    response = client.post("/api/plugins/kanban/master/tasks", json={"title": "blocked"})
    assert response.status_code == 409
    assert response.json()["detail"]["error"] == "kanban_env_collapse"
