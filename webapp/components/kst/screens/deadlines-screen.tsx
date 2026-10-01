'use client'

import { Button, Placeholder } from '@telegram-apps/telegram-ui'
import { BookOpen, CalendarClock, Check, GraduationCap, Plus } from 'lucide-react'
import {
  dateParts,
  getUrgency,
  lessonsLabel,
  relativeLabel,
  type Deadline,
  type Urgency,
} from '@/lib/kst-data'
import { haptic } from '@/lib/telegram'
import { cn } from '@/lib/utils'

const GROUPS: { key: Urgency; label: string; dot: string; text: string; tile: string }[] = [
  {
    key: 'overdue',
    label: 'Просрочено',
    dot: 'bg-tg-destructive',
    text: 'text-tg-destructive',
    tile: 'bg-tg-destructive/12 text-tg-destructive',
  },
  { key: 'today', label: 'Сегодня', dot: 'bg-orange-500', text: 'text-orange-500', tile: 'bg-orange-500/12 text-orange-500' },
  {
    key: 'tomorrow',
    label: 'Завтра',
    dot: 'bg-yellow-400',
    text: 'text-yellow-600 dark:text-yellow-400',
    tile: 'bg-yellow-400/15 text-yellow-600 dark:text-yellow-400',
  },
  { key: 'later', label: 'Позже', dot: 'bg-tg-link', text: 'text-tg-hint', tile: 'bg-tg-link/12 text-tg-link' },
]

type Props = {
  deadlines: Deadline[]
  onAdd: () => void
  onToggleDone: (id: string) => void
}

export function DeadlinesScreen({ deadlines, onAdd, onToggleDone }: Props) {
  const sorted = [...deadlines].sort((a, b) => a.dueISO.localeCompare(b.dueISO))

  return (
    <div className="flex flex-col gap-5 px-4 pt-4">
      <Button stretched size="l" mode="filled" before={<Plus className="size-5" aria-hidden="true" />} onClick={onAdd}>
        Добавить дедлайн
      </Button>

      {sorted.length === 0 && <Placeholder header="Все задачи сданы" description="Новых дедлайнов пока нет" />}

      {GROUPS.map((group) => {
        const items = sorted.filter((d) => getUrgency(d.dueISO) === group.key)
        if (items.length === 0) return null
        return (
          <section key={group.key} className="flex flex-col gap-2" aria-label={group.label}>
            <h2 className={cn('flex items-center gap-2 px-1 text-sm font-semibold', group.text)}>
              <span className={cn('size-2 rounded-full', group.dot)} aria-hidden="true" />
              {group.label}
              <span className="font-medium text-tg-hint">{items.length}</span>
            </h2>
            {items.map((deadline) => (
              <DeadlineCard
                key={deadline.id}
                deadline={deadline}
                tileClass={group.tile}
                onDone={() => {
                  haptic()
                  onToggleDone(deadline.id)
                }}
              />
            ))}
          </section>
        )
      })}
    </div>
  )
}

function DeadlineCard({ deadline, tileClass, onDone }: { deadline: Deadline; tileClass: string; onDone: () => void }) {
  const parts = dateParts(deadline.dueISO)
  const primary =
    deadline.dueMode === 'lessons' && deadline.dueLessons ? lessonsLabel(deadline.dueLessons) : relativeLabel(deadline.dueISO)
  const secondary =
    deadline.dueMode === 'lessons'
      ? `${relativeLabel(deadline.dueISO)}${deadline.dueLessonNumber ? ` · ${deadline.dueLessonNumber} пара` : ''}`
      : parts.long

  return (
    <article className="flex gap-3 rounded-2xl bg-tg-section p-3.5 shadow-[0_1px_2px_rgba(0,0,0,0.06)]">
      <div className={cn('flex w-14 shrink-0 flex-col items-center justify-center rounded-xl py-2', tileClass)}>
        <span className="text-[10px] font-semibold uppercase tracking-wider opacity-80">{parts.weekday}</span>
        <span className="text-2xl font-bold leading-none tabular-nums">{parts.day}</span>
        <span className="text-[11px] font-medium">{parts.month}</span>
      </div>

      <div className="flex min-w-0 flex-1 flex-col gap-1.5">
        <h3 className="text-[16px] font-semibold leading-snug text-pretty">{deadline.title}</h3>

        <div className="flex flex-col gap-1 text-[13px]">
          <span className="flex items-center gap-1.5 font-medium text-tg-text">
            <BookOpen className="size-3.5 shrink-0 text-tg-link" aria-hidden="true" />
            <span className="truncate">{deadline.subject}</span>
          </span>
          <span className="flex items-center gap-1.5 text-tg-subtitle">
            <GraduationCap className="size-3.5 shrink-0" aria-hidden="true" />
            <span className="truncate">{deadline.teacher}</span>
          </span>
        </div>

        <div className="mt-0.5 flex flex-wrap items-center gap-x-2 gap-y-1">
          <span className="inline-flex items-center gap-1 rounded-full bg-tg-fill px-2 py-0.5 text-xs font-semibold text-tg-text">
            <CalendarClock className="size-3.5" aria-hidden="true" />
            {primary}
          </span>
          <span className="text-xs text-tg-hint first-letter:uppercase">{secondary}</span>
        </div>
      </div>

      <button
        type="button"
        onClick={onDone}
        className="flex size-9 shrink-0 items-center justify-center self-center rounded-full border-2 border-tg-divider text-tg-hint transition-colors hover:border-tg-success hover:bg-tg-success hover:text-white"
        aria-label={`Отметить «${deadline.title}» выполненным`}
      >
        <Check className="size-4" aria-hidden="true" />
      </button>
    </article>
  )
}
