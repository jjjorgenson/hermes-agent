"""Kanban task serialization — one canonical shape for board and Master surfaces.

``serialize_task(board_slug, task, ...)`` is the single envelope later read and
write paths share: full task fields plus board identity and display metadata.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any, Optional

from hermes_cli import kanban_db

_CARD_SUMMARY_PREVIEW_CHARS = 200


def serialize_task(
    board_slug: str,
    task: kanban_db.Task,
    *,
    board_meta: Optional[dict[str, Any]] = None,
    latest_summary: Optional[str] = None,
    summary_preview_chars: int = _CARD_SUMMARY_PREVIEW_CHARS,
    extras: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Full task row + board_slug + board.json name/icon/color + worktree fields.

    ``write_safe_root`` is intentionally omitted — it is spawn-derived only and
    must never appear as a client field.
    """
    meta = board_meta if board_meta is not None else kanban_db.read_board_metadata(board_slug)
    d: dict[str, Any] = asdict(task)
    d["board_slug"] = board_slug
    d["board_name"] = meta.get("name") or board_slug
    d["board_icon"] = meta.get("icon") or ""
    d["board_color"] = meta.get("color") or ""
    # project_id on the task row is worktree anchoring; board scope is separate.
    if task.project_id:
        d["project_id"] = task.project_id
    if task.branch_name:
        d["branch_name"] = task.branch_name
    try:
        d["age"] = kanban_db.task_age(task)
    except Exception:
        d["age"] = {
            "created_age_seconds": None,
            "started_age_seconds": None,
            "time_to_complete_seconds": None,
        }
    preview = latest_summary
    if preview and summary_preview_chars and len(preview) > summary_preview_chars:
        preview = preview[:summary_preview_chars]
    d["latest_summary"] = preview
    if extras:
        d.update(extras)
    return d


def task_address(board_slug: str, task_id: str) -> dict[str, str]:
    """Composite key every Master client and event must carry."""
    return {"board_slug": board_slug, "task_id": task_id}
