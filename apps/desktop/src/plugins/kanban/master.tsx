/**
 * Master kanban leadership view — read-only fleet window across all boards.
 * Cards are color-coded by board.json metadata; click opens that board.
 */

import {
  Button,
  cn,
  Codicon,
  ErrorState,
  host,
  Loader,
  SearchField,
  Switch,
  Tip,
  useGrabScroll,
  useQuery,
  useValue
} from '@hermes/plugin-sdk'
import { type CSSProperties, useMemo, useRef, useState } from 'react'

import { $boardSlug, fetchMasterCapability, fetchMasterTasks, MASTER_KEY } from './api'
import { columnLabel, shortId, useKanban } from './ui'
import { columnMeta, type KanbanMasterTask, type MasterTasksResponse } from './types'

function boardAccent(task: KanbanMasterTask): string {
  return task.board_color?.trim() || columnMeta(task.status).tone
}

function MasterCard({ task, onOpen }: { task: KanbanMasterTask; onOpen: (task: KanbanMasterTask) => void }) {
  const k = useKanban()
  const accent = boardAccent(task)
  const summary = task.latest_summary || task.body

  return (
    <button
      className={cn(
        'group relative flex w-full flex-col gap-2 rounded-md border border-(--ui-stroke-tertiary) border-l-[3px] bg-(--ui-bg-elevated) p-2.5 text-left',
        'transition-colors hover:bg-primary/[0.06]'
      )}
      onClick={() => onOpen(task)}
      style={{ borderLeftColor: accent } as CSSProperties}
      type="button"
    >
      <div className="flex items-center gap-1.5 text-[0.625rem] text-(--ui-text-tertiary)">
        {task.board_icon ? (
          <Codicon name={task.board_icon as 'project'} size="0.7rem" style={{ color: accent }} />
        ) : (
          <span className="size-2 shrink-0 rounded-full" style={{ backgroundColor: accent }} />
        )}
        <span className="truncate font-medium">{task.board_name || task.board_slug}</span>
      </div>
      <span className="line-clamp-2 text-[0.8125rem] font-medium leading-snug text-foreground">
        {task.title || task.id}
      </span>
      {summary && (
        <span className="line-clamp-2 text-[0.6875rem] leading-snug text-(--ui-text-tertiary)">{summary}</span>
      )}
      <div className="flex items-center gap-2 text-[0.625rem] text-(--ui-text-quaternary)">
        {task.assignee && <span className="truncate">{task.assignee}</span>}
        <span className="ml-auto font-mono">{shortId(task.id)}</span>
      </div>
      <span className="sr-only">{k.masterOpenBoard(task.board_name || task.board_slug)}</span>
    </button>
  )
}

function MasterColumn({
  column,
  onOpen
}: {
  column: MasterTasksResponse['columns'][number]
  onOpen: (task: KanbanMasterTask) => void
}) {
  const k = useKanban()
  const meta = columnMeta(column.name)

  return (
    <section className="flex w-[17rem] shrink-0 flex-col gap-2">
      <header className="flex items-center gap-1.5 px-1 text-[0.6875rem] font-medium uppercase tracking-wide text-(--ui-text-tertiary)">
        <Codicon name={meta.codicon as 'circle-outline'} size="0.75rem" style={{ color: meta.tone }} />
        {columnLabel(k, column.name as keyof typeof k.col)}
        <span className="ml-auto tabular-nums">{column.tasks.length}</span>
      </header>
      <div className="flex flex-col gap-2">
        {column.tasks.map(task => (
          <MasterCard key={`${task.board_slug}:${task.id}`} onOpen={onOpen} task={task} />
        ))}
        {column.tasks.length === 0 && (
          <div className="rounded-md border border-dashed border-(--ui-stroke-tertiary) px-3 py-6 text-center text-[0.6875rem] text-(--ui-text-quaternary)">
            {k.empty}
          </div>
        )}
      </div>
    </section>
  )
}

export function KanbanMasterPage() {
  const k = useKanban()
  const [includeTodoTriage, setIncludeTodoTriage] = useState(false)
  const [search, setSearch] = useState('')

  const { data: capability } = useQuery({
    queryKey: ['kanban', 'master', 'capability'],
    queryFn: fetchMasterCapability,
    staleTime: 60_000
  })

  const { data, error, isLoading } = useQuery({
    queryKey: MASTER_KEY(includeTodoTriage),
    queryFn: () => fetchMasterTasks(includeTodoTriage),
    refetchInterval: 30_000
  })

  const filtered = useMemo(() => {
    if (!data) {
      return null
    }

    const q = search.trim().toLowerCase()
    if (!q) {
      return data
    }

    const keep = (task: KanbanMasterTask) =>
      `${task.title} ${task.body ?? ''} ${task.id} ${task.board_slug} ${task.board_name ?? ''}`
        .toLowerCase()
        .includes(q)

    return {
      ...data,
      columns: data.columns.map(col => ({ ...col, tasks: col.tasks.filter(keep) }))
    }
  }, [data, search])

  const scrollRef = useRef<HTMLDivElement>(null)
  const { grabbing, onMouseDown } = useGrabScroll(scrollRef)

  const openBoard = (task: KanbanMasterTask) => {
    $boardSlug.set(task.board_slug)
    host.navigate('/kanban')
  }

  if (error) {
    return <ErrorState message={String(error)} title={k.masterTitle} />
  }

  return (
    <div className="flex h-full min-h-0 flex-col gap-3 p-3">
      <header className="flex flex-wrap items-center gap-2">
        <div className="mr-auto flex min-w-0 items-center gap-2">
          <Codicon name="globe" size="1rem" />
          <div>
            <h1 className="text-[0.9375rem] font-semibold">{k.masterTitle}</h1>
            <p className="text-[0.6875rem] text-(--ui-text-tertiary)">{k.masterSubtitle}</p>
          </div>
        </div>
        <Button onClick={() => host.navigate('/kanban')} size="xs" variant="ghost">
          <Codicon name="project" size="0.8rem" />
          {k.masterBoardView}
        </Button>
        <SearchField
          aria-label={k.filterCards}
          className="w-[12rem]"
          onChange={event => setSearch(event.target.value)}
          placeholder={k.filterCards}
          value={search}
        />
        <label className="flex items-center gap-2 text-[0.6875rem] text-(--ui-text-secondary)">
          <Switch
            aria-label={k.masterShowTodoTriage}
            checked={includeTodoTriage}
            onCheckedChange={setIncludeTodoTriage}
            size="xs"
          />
          {k.masterShowTodoTriage}
        </label>
        {capability?.capability === 'read' && (
          <Tip label={k.masterReadOnlyTip}>
            <span className="rounded bg-(--ui-control-active-background) px-2 py-0.5 text-[0.625rem] uppercase tracking-wide text-(--ui-text-tertiary)">
              {k.masterReadOnly}
            </span>
          </Tip>
        )}
      </header>

      {data?.warnings?.length ? (
        <div className="rounded-md border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-[0.6875rem] text-amber-200">
          {data.warnings.map(w => (
            <p key={w}>{w}</p>
          ))}
        </div>
      ) : null}

      {data?.errors?.length ? (
        <div className="rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-[0.6875rem] text-destructive">
          {data.errors.map(entry => (
            <p key={entry.board_slug}>
              {entry.board_slug}: {entry.error}
            </p>
          ))}
        </div>
      ) : null}

      {isLoading && !filtered ? (
        <div className="flex flex-1 items-center justify-center">
          <Loader />
        </div>
      ) : (
        <div className="min-h-0 flex-1 overflow-hidden">
          <div
            className={cn('kanban-board-scroll flex h-full gap-3 overflow-x-auto pb-2', grabbing && 'cursor-grabbing')}
            onMouseDown={onMouseDown}
            ref={scrollRef}
          >
            {(filtered?.columns ?? []).map(col => (
              <MasterColumn column={col} key={col.name} onOpen={openBoard} />
            ))}
          </div>
        </div>
      )}
    </div>
  )
}
