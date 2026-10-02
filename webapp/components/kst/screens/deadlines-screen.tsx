'use client'

import { useState } from 'react'
import { Button, Placeholder } from '@telegram-apps/telegram-ui'
import { BookOpen, CalendarClock, Check, GraduationCap, Plus, RefreshCw } from 'lucide-react'

import type { Deadline } from '@/lib/api-types'
import { apiDelete } from '@/lib/api'
import { dateParts, relativeLabel } from '@/lib/kst-format'
import { useApi } from '@/lib/use-api'
import { haptic, notify } from '@/lib/telegram'
import { cn } from '@/lib/utils'
import { ErrorState, SkeletonList } from '../async-state'

/** Группировка дедлайнов по срочности. Ключ считается из `days_left`. */
type Urgency = 'overdue' | 'today' | 'tomorrow' | 'later' | 'none'

const GROUPS: { key: Urgency; label: string; dot: string; text: string; tile: string }[] = [
  {
    key: 'overdue',
    label: 'Просрочено',
    dot: 'bg-tg-danger',
    text: 'text-tg-danger',
    tile: 'bg-tg-danger/15 text-tg-danger',
  },
  {
    key: 'today',
    label: 'Сегодня',
    dot: 'bg-tg-warning',
    text: 'text-tg-warning',
    tile: 'bg-tg-warning/15 text-tg-warning',
  },
  {
    key: 'tomorrow',
    label: 'Завтра',
    dot: 'bg-tg-warning',
    text: 'text-tg-warning',
    tile: 'bg-tg-warning/15 text-tg-warning',
  },
  {
    key: 'later',
    label: 'Позже',
    dot: 'bg-tg-link',
    text: 'text-tg-hint',
    tile: 'bg-tg-link/12 text-tg-link',
  },
  {
    key: 'none',
    label: 'Без срока',
    dot: 'bg-tg-hint',
    text: 'text-tg-hint',
    tile: 'bg-tg-fill text-tg-hint',
  },
]

/** Срочность по числу дней до срока (null — срока нет). */
function urgencyOf(daysLeft: number | null): Urgency {
  if (daysLeft === null) return 'none'
  if (daysLeft < 0) return 'overdue'
  if (daysLeft === 0) return 'today'
  if (daysLeft === 1) return 'tomorrow'
  return 'later'
}

/**
 * Список дедлайнов студента.
 *
 * Данные: ``/api/deadlines``. Удаление — ``DELETE /api/deadlines/{id}``: на
 * бэкенде это soft delete, запись помечается удалённой (история сохраняется).
 */
export function DeadlinesScreen({ onAdd }: { onAdd: () => void }) {
  const { data, loading, error, refresh } = useApi<{ items: Deadline[] }>('/api/deadlines')
  const [removing, setRemoving] = useState<number | null>(null)

  if (loading && !data) return <SkeletonList rows={3} />
  if (error && !data) return <ErrorState message={error} onRetry={refresh} />

  // Порядок как на бэкенде: бездатные в конец, остальные по сроку.
  const items = [...(data?.items ?? [])].sort((a, b) => {
    if (!a.date && !b.date) return 0
    if (!a.date) return 1
    if (!b.date) return -1
    return a.date.localeCompare(b.date)
  })

  const remove = async (id: number) => {
    setRemoving(id)
    try {
      await apiDelete(`/api/deadlines/${id}`)
      refresh()
    } catch {
      notify('Не удалось удалить дедлайн. Попробуйте ещё раз.')
    } finally {
      setRemoving(null)
    }
  }

  return (
    <div className="flex flex-col gap-5 px-4 pt-4">
      <Button stretched size="l" mode="filled" before={<Plus className="size-5" aria-hidden="true" />} onClick={onAdd}>
        Добавить дедлайн
      </Button>

      {items.length === 0 && <Placeholder header="Все задачи сданы" description="Новых дедлайнов пока нет" />}

      {GROUPS.map((group) => {
        const groupItems = items.filter((item) => urgencyOf(item.days_left) === group.key)
        if (groupItems.length === 0) return null
        return (
          <section key={group.key} className="flex flex-col gap-2" aria-label={group.label}>
            <h2 className={cn('flex items-center gap-2 px-1 text-sm font-semibold', group.text)}>
              <span className={cn('size-2 rounded-full', group.dot)} aria-hidden="true" />
              {group.label}
              <span className="font-medium text-tg-hint">{groupItems.length}</span>
            </h2>
            {groupItems.map((deadline) => (
              <DeadlineCard
                key={deadline.id}
                deadline={deadline}
                tileClass={group.tile}
                pending={removing === deadline.id}
                onDone={() => {
                  haptic()
                  void remove(deadline.id)
                }}
              />
            ))}
          </section>
        )
      })}

      {items.length > 0 && (
        <Button
          stretched
          size="l"
          mode="bezeled"
          before={<RefreshCw className="size-5" aria-hidden="true" />}
          onClick={refresh}
        >
          Обновить
        </Button>
      )}
    </div>
  )
}

function DeadlineCard({
  deadline,
  tileClass,
  pending,
  onDone,
}: {
  deadline: Deadline
  tileClass: string
  pending: boolean
  onDone: () => void
}) {
  const parts = deadline.date ? dateParts(deadline.date) : null

  return (
    <article className="flex gap-3 rounded-2xl bg-tg-section p-3.5 shadow-[0_1px_2px_rgba(0,0,0,0.06)]">
      <div className={cn('flex w-14 shrink-0 flex-col items-center justify-center rounded-xl py-2', tileClass)}>
        {parts ? (
          <>
            <span className="text-[10px] font-semibold tracking-wider uppercase opacity-80">{parts.weekdayShort}</span>
            <span className="text-2xl leading-none font-bold tabular-nums">{parts.day}</span>
            <span className="text-[11px] font-medium">{parts.month}</span>
          </>
        ) : (
          <span className="text-xs font-semibold">—</span>
        )}
      </div>

      <div className="flex min-w-0 flex-1 flex-col gap-1.5">
        <h3 className="text-[16px] leading-snug font-semibold text-pretty">{deadline.task}</h3>

        <div className="flex flex-col gap-1 text-[13px]">
          {deadline.subject && (
            <span className="flex items-center gap-1.5 font-medium text-tg-text">
              <BookOpen className="size-3.5 shrink-0 text-tg-link" aria-hidden="true" />
              <span className="truncate">{deadline.subject}</span>
            </span>
          )}
          {deadline.teacher && (
            <span className="flex items-center gap-1.5 text-tg-subtitle">
              <GraduationCap className="size-3.5 shrink-0" aria-hidden="true" />
              <span className="truncate">{deadline.teacher}</span>
            </span>
          )}
        </div>

        <div className="mt-0.5 flex flex-wrap items-center gap-x-2 gap-y-1">
          <span className="inline-flex items-center gap-1 rounded-full bg-tg-fill px-2 py-0.5 text-xs font-semibold text-tg-text">
            <CalendarClock className="size-3.5" aria-hidden="true" />
            {relativeLabel(deadline.days_left)}
          </span>
          {parts && <span className="text-xs text-tg-hint first-letter:uppercase">{parts.long}</span>}
        </div>
      </div>

      <button
        type="button"
        onClick={onDone}
        disabled={pending}
        className="flex size-9 shrink-0 items-center justify-center self-center rounded-full border-2 border-tg-divider text-tg-hint transition-colors hover:border-tg-success hover:bg-tg-success hover:text-white disabled:opacity-50"
        aria-label={`Отметить «${deadline.task}» выполненным`}
      >
        <Check className="size-4" aria-hidden="true" />
      </button>
    </article>
  )
}
