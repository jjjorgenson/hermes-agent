"""Master kanban capability ladder and global-env isolation guards."""

from __future__ import annotations

import os
from typing import Any

from fastapi import HTTPException

MASTER_CAPABILITIES = ("read", "move", "full-edit")
_CAPABILITY_RANK = {name: idx for idx, name in enumerate(MASTER_CAPABILITIES)}

# Reserved for cross-board transfer writes (future slices).
RESERVED_EVENT_KINDS = ("transferred_to", "transferred_from")

# Reserved workspace policy names for transfer routing (future slices).
WORKSPACE_POLICIES = ("destination_default", "fresh_worktree", "refuse_if_worktree")

_COLLAPSING_ENV_VARS = (
    "HERMES_KANBAN_DB",
    "HERMES_KANBAN_WORKSPACES_ROOT",
    "HERMES_KANBAN_ATTACHMENTS_ROOT",
)


def _load_kanban_dashboard_cfg() -> dict[str, Any]:
    try:
        from hermes_cli.config import load_config

        cfg = load_config() or {}
    except Exception:
        return {}
    section = (cfg.get("dashboard") or {}).get("kanban") or {}
    return section if isinstance(section, dict) else {}


def get_master_capability() -> str:
    """``dashboard.kanban.master.capability`` — read | move | full-edit (default read)."""
    raw = str((_load_kanban_dashboard_cfg().get("master") or {}).get("capability") or "read").strip()
    if raw not in _CAPABILITY_RANK:
        return "read"
    return raw


def capability_allows(current: str, required: str) -> bool:
    """Ladder: read ⊂ move ⊂ full-edit."""
    cur = _CAPABILITY_RANK.get(current, 0)
    req = _CAPABILITY_RANK.get(required, 0)
    return cur >= req


def master_capability_error(required: str, current: str | None = None) -> HTTPException:
    current = current or get_master_capability()
    return HTTPException(
        status_code=403,
        detail={"error": "master_capability", "required": required, "current": current},
    )


def require_master_capability(required: str) -> None:
    current = get_master_capability()
    if not capability_allows(current, required):
        raise master_capability_error(required, current)


def collapsing_env_warnings() -> list[str]:
    """Global pins that collapse multi-board isolation — warn on every Master touch."""
    warnings: list[str] = []
    for var in _COLLAPSING_ENV_VARS:
        if os.environ.get(var, "").strip():
            warnings.append(
                f"{var} is set in the process environment; multi-board Master views "
                "resolve a single database/workspace tree. Unset it for fleet-wide kanban."
            )
    return warnings


def reject_if_collapsing_env() -> None:
    """409 when a Master mutator would run under collapsed isolation."""
    warnings = collapsing_env_warnings()
    if warnings:
        raise HTTPException(status_code=409, detail={"error": "kanban_env_collapse", "warnings": warnings})
