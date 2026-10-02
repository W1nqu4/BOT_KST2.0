'use client'

import { useEffect, useState } from 'react'
import { Accordion, Cell, List, Section } from '@telegram-apps/telegram-ui'

import type { WeekPayload } from '@/lib/api-types'
import { useApi } from '@/lib/use-api'
import { plural } from '@/lib/kst-format'
import { cn } from '@/lib/utils'
import { STATUS_META } from '../lesson-status'
import { ErrorState, SkeletonList } from '../async-state'

/** Расписание на неделю (Пн–Сб) в аккордеоне. */
export function WeekScreen() {
  const { data, loading, error, errorCode, refresh } = useApi<WeekPayload>('/api/schedule/week')
  const [expanded, setExpanded] = useState<Set<string>>(new Set())
  const [initialised, setInitialised] = useState(false)

  // Раскрываем сегодняшний день, как только данные пришли (в эффекте, а не в
  // рендере: обновление состояния во время рендера — источник лишних ререндеров).
  useEffect(() => {
    if (initialised || !data?.days) return
    const today = data.days.find((day) => day.is_today)
    setExpanded(new Set(today ? [today.date] : []))
    setInitialised(true)
  }, [data, initialised])

  if (errorCode === 'group_not_set') {
    return <ErrorState message="Укажите группу в чате с ботом — тогда появится расписание." />
  }
  if (loading && !data) return <SkeletonList rows={4} />
  if (error && !data) return <ErrorState message={error} onRetry={refresh} />

  const days = data?.days ?? []
  if (days.length === 0) {
    return <ErrorState message="Расписание пустое. Загляните позже." onRetry={refresh} />
  }

  const toggle = (key: string, open: boolean) =>
    setExpanded((prev) => {
      const next = new Set(prev)
      if (open) next.add(key)
      else next.delete(key)
      return next
    })

  return (
    <List>
      <Section footer="Нажмите на день, чтобы свернуть или развернуть список пар">
        {days.map((day) => {
          const lessons = day.lessons
          return (
            <Accordion key={day.date} expanded={expanded.has(day.date)} onChange={(open) => toggle(day.date, open)}>
              <Accordion.Summary
                subtitle={`${day.date} · ${lessons.length} ${plural(lessons.length, 'пара', 'пары', 'пар')}`}
                after={
                  day.is_today ? (
                    <span className="rounded-full bg-tg-fill px-2 py-0.5 text-xs font-semibold text-tg-link">Сегодня</span>
                  ) : undefined
                }
              >
                <span className="font-semibold">{day.weekday}</span>
              </Accordion.Summary>

              <Accordion.Content>
                <div className="flex flex-col pb-2">
                  {lessons.length === 0 && <p className="px-4 py-3 text-sm text-tg-hint">Пар нет</p>}
                  {lessons.map((lesson) => {
                    const meta = STATUS_META[lesson.status]
                    const Icon = meta.icon
                    return (
                      <Cell
                        key={lesson.para}
                        subtitle={lesson.teacher || undefined}
                        before={
                          <span className="flex size-7 items-center justify-center rounded-lg bg-tg-secondary-bg text-sm font-bold tabular-nums">
                            {lesson.para}
                          </span>
                        }
                        after={
                          <span className="flex items-center gap-2 text-sm text-tg-hint">
                            {lesson.room}
                            {lesson.status !== 'planned' && (
                              <Icon className={cn('size-4', meta.textClassName)} aria-label={meta.label} />
                            )}
                          </span>
                        }
                      >
                        <span className={cn(lesson.is_cancelled && 'text-tg-hint line-through')}>{lesson.subject}</span>
                      </Cell>
                    )
                  })}
                </div>
              </Accordion.Content>
            </Accordion>
          )
        })}
      </Section>
    </List>
  )
}
