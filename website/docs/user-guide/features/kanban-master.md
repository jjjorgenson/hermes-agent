# Kanban Master view

The **Master** kanban surface is a leadership window across every board on the
host. It is not a third workplace: each board remains the hard isolation
boundary (its own SQLite file, workspaces, and attachments). Master is a
read-mostly facade that fans out across boards and paints one fleet-wide picture.

## Capability ladder

Configure in `config.yaml`:

```yaml
dashboard:
  kanban:
    master:
      capability: read   # read | move | full-edit (default: read)
```

| Level | Master GET | Master move | Master create / full edit |
|-------|------------|-------------|---------------------------|
| `read` | yes | 403 | 403 |
| `move` | yes | yes (future slice) | 403 |
| `full-edit` | yes | yes | yes (future slice) |

Mutating Master routes share the same handler spine as board routes. When
capability is insufficient the API returns **403** with:

```json
{"error": "master_capability", "required": "move", "current": "read"}
```

## Addressing and events

Every task is addressed as **`(board_slug, task_id)`**. Master REST payloads,
WebSocket frames, and UI keys always carry `board_slug`. Click-through URLs are
board-scoped — never a bare task id.

WebSocket: `/api/plugins/kanban/master/events` — one channel; each event includes
`board_slug`.

## Serialization

`hermes_cli.kanban_serialize.serialize_task(board, row)` is the canonical
envelope for Master (and future Master writes): full task fields plus
`board_slug`, `board_name`, `board_icon`, `board_color`, and worktree fields
when present. `write_safe_root` is spawn-derived only and is never sent to
clients.

## Global env pins (multi-board collapse)

Do **not** set these in a shared shell or `.env` when running multiple boards:

- `HERMES_KANBAN_DB`
- `HERMES_KANBAN_WORKSPACES_ROOT`
- `HERMES_KANBAN_ATTACHMENTS_ROOT`

Master GET responses include **warnings** when any are set. Master mutators
return **409** (`kanban_env_collapse`) so a collapsed host cannot accept
cross-board writes.

## Reserved for later slices

**Event kinds** (not emitted in the read slice): `transferred_to`,
`transferred_from`.

**Workspace policy enum** (transfer routing): `destination_default`,
`fresh_worktree`, `refuse_if_worktree`.

**Redirect index**: `GET /api/plugins/kanban/master/redirect-index` returns an
empty schema today (`{"redirects": []}`).

## Desktop UI

Enable the kanban plugin, open **Master view** from the board header or
`/kanban/master`. Default columns: Ready, Running, Blocked, Review. Toggle
**Show todo & triage** for upstream lanes. Cards are color-coded from each
board's `board.json`; click a card to open that board.

At `read` capability there is no create, drag-move, or edit chrome on Master.
