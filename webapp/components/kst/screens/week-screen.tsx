'use client'

import { useState } from 'react'
import { Accordion, List, Section, Cell } from '@telegram-apps/telegram-ui'
import { DAYS, SCHEDULE, TODAY_KEY, type DayKey } from '@/lib/kst-data'
import { cn } from '@/lib/utils'
import { STATUS_META } from '../lesson-status'

export function WeekScreen() {
  const [expanded, setExpanded] = useState<Set<DayKey>>(() => new Set([TODAY_KEY]))

  const toggle = (key: DayKey, open: boolean) =>
    setExpanded((prev) => {
      const next = new Set(prev)
      if (open) next.add(key)
      else next.delete(key)
      return next
    })

  return (
    <List>
      <Section footer="Нажмите на день, чтобы свернуть или развернуть список пар">
        {DAYS.map((day) => {
          const lessons = SCHEDULE[day.key]
          return (
            <Accordion key={day.key} expanded={expanded.has(day.key)} onChange={(open) => toggle(day.key, open)}>
              <Accordion.Summary
                subtitle={`${day.date} · ${lessons.length} пар`}
                after={
                  day.key === TODAY_KEY ? (
                    <span className="rounded-full bg-tg-fill px-2 py-0.5 text-xs font-semibold text-tg-link">
                      Сегодня
                    </span>
                  ) : undefined
                }
              >
                <span className="font-semibold">{day.full}</span>
              </Accordion.Summary>
              <Accordion.Content>
                <div className="flex flex-col pb-2">
                  {lessons.map((lesson) => {
                    const meta = STATUS_META[lesson.status]
                    const Icon = meta.icon
                    return (
                      <Cell
                        key={lesson.number}
                        before={
                          <span className="flex size-7 items-center justify-center rounded-lg bg-tg-secondary-bg text-sm font-bold tabular-nums">
                            {lesson.number}
                          </span>
                        }
                        after={
                          <span className="flex items-center gap-2 text-sm text-tg-hint">
                            {lesson.room}
                            {lesson.status !== 'plan' && (
                              <Icon className={cn('size-4', meta.textClassName)} aria-label={meta.label} />
                            )}
                          </span>
                        }
                      >
                        <span className={cn(lesson.status === 'cancel' && 'text-tg-hint line-through')}>
                          {lesson.subject}
                        </span>
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
