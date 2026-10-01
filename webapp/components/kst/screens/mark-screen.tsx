'use client'

import { useState } from 'react'
import { Button, List, Section } from '@telegram-apps/telegram-ui'
import { MARK_LABEL, SCHEDULE, TODAY_KEY, type GroupMember, type MarkStatus } from '@/lib/kst-data'
import { haptic, notify } from '@/lib/telegram'
import { cn } from '@/lib/utils'

const STATUSES: MarkStatus[] = ['present', 'late', 'absent', 'excused']

const ACTIVE_TONE: Record<MarkStatus, string> = {
  present: 'bg-tg-success text-white',
  late: 'bg-tg-warning text-white',
  absent: 'bg-tg-destructive text-white',
  excused: 'bg-tg-link text-white',
}

const INITIAL: MarkStatus[] = ['present', 'present', 'present', 'absent', 'late', 'present', 'absent', 'present']

export function MarkScreen({ members }: { members: GroupMember[] }) {
  const lessons = SCHEDULE[TODAY_KEY].filter((l) => l.status !== 'cancel')
  const [lessonNumber, setLessonNumber] = useState(lessons[0]?.number ?? 1)
  const [marks, setMarks] = useState<Record<string, MarkStatus>>(() =>
    Object.fromEntries(members.map((m, i) => [m.id, INITIAL[i % INITIAL.length]])),
  )

  const lesson = lessons.find((l) => l.number === lessonNumber)
  const count = (s: MarkStatus) => Object.values(marks).filter((v) => v === s).length

  const setMark = (id: string, status: MarkStatus) => {
    haptic()
    setMarks((m) => ({ ...m, [id]: status }))
  }

  return (
    <List>
      <Section header="Пара">
        <div className="flex gap-2 overflow-x-auto p-3" role="radiogroup" aria-label="Выбор пары">
          {lessons.map((l) => (
            <button
              key={l.number}
              type="button"
              role="radio"
              aria-checked={l.number === lessonNumber}
              onClick={() => setLessonNumber(l.number)}
              className={cn(
                'shrink-0 rounded-xl px-3 py-2 text-left transition-colors',
                l.number === lessonNumber ? 'bg-tg-button text-tg-button-text' : 'bg-tg-secondary-bg',
              )}
            >
              <span className="block text-xs opacity-75">{l.number} пара</span>
              <span className="block max-w-36 truncate text-sm font-semibold">{l.subject}</span>
            </button>
          ))}
        </div>
        <div className="grid grid-cols-4 gap-1 border-t border-tg-divider px-3 py-2.5 text-center text-xs text-tg-hint">
          {STATUSES.map((s) => (
            <span key={s}>
              <span className="block text-base font-bold text-tg-text tabular-nums">{count(s)}</span>
              {MARK_LABEL[s]}
            </span>
          ))}
        </div>
      </Section>

      <Section header={`Студенты · ${members.length}`} footer="Отметка старосты переопределяет ответ студента в опросе.">
        {members.map((m) => (
          <div key={m.id} className="flex flex-col gap-2 border-b border-tg-divider px-4 py-3 last:border-b-0">
            <span className="text-[15px] font-medium">{m.name}</span>
            <div className="grid grid-cols-4 gap-1.5" role="radiogroup" aria-label={`Статус: ${m.name}`}>
              {STATUSES.map((s) => (
                <button
                  key={s}
                  type="button"
                  role="radio"
                  aria-checked={marks[m.id] === s}
                  onClick={() => setMark(m.id, s)}
                  className={cn(
                    'h-8 rounded-lg text-xs font-semibold transition-colors',
                    marks[m.id] === s ? ACTIVE_TONE[s] : 'bg-tg-secondary-bg text-tg-hint',
                  )}
                >
                  {MARK_LABEL[s]}
                </button>
              ))}
            </div>
          </div>
        ))}
      </Section>

      <div className="px-1">
        <Button stretched size="l" onClick={() => notify(`Отметки за ${lessonNumber} пару (${lesson?.subject}) сохранены.`)}>
          Сохранить отметки
        </Button>
      </div>
    </List>
  )
}
