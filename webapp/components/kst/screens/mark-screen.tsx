'use client'

import { useState } from 'react'
import { Button, List, Section } from '@telegram-apps/telegram-ui'
import { MARK_LABEL, type GroupMember, type MarkStatus } from '@/lib/kst-data'
import type { ScheduleDay } from '@/lib/api-types'
import { useApi } from '@/lib/use-api'
import { haptic, notify } from '@/lib/telegram'
import { cn } from '@/lib/utils'

const STATUSES: MarkStatus[] = ['present', 'late', 'absent', 'excused']

const ACTIVE_TONE: Record<MarkStatus, string> = {
  present: 'bg-tg-success text-white',
  late: 'bg-tg-warning text-white',
  absent: 'bg-tg-danger text-white',
  excused: 'bg-tg-link text-white',
}

const INITIAL: MarkStatus[] = ['present', 'present', 'present', 'absent', 'late', 'present', 'absent', 'present']

/**
 * Ручная отметка явки (экран старосты).
 *
 * Список пар берётся из ``/api/schedule/today`` — реальный день с замены и
 * отменами; отменённые пары исключены, отмечать по ним нечего.
 *
 * ⚠️ Список студентов и сами отметки пока демонстрационные: серверного
 * эндпоинта для группы нет (в боте отметку ставит староста кнопками в чате).
 * Поэтому изменения живут только в состоянии экрана.
 */
export function MarkScreen({ members }: { members: GroupMember[] }) {
  const { data } = useApi<ScheduleDay>('/api/schedule/today')
  const lessons = (data?.lessons ?? []).filter((lesson) => !lesson.is_cancelled)

  const [selectedPara, setSelectedPara] = useState<number | null>(null)
  const [marks, setMarks] = useState<Record<string, MarkStatus>>(() =>
    Object.fromEntries(members.map((member, index) => [member.id, INITIAL[index % INITIAL.length]])),
  )

  const activePara = selectedPara ?? lessons[0]?.para ?? 1
  const lesson = lessons.find((item) => item.para === activePara)
  const count = (status: MarkStatus) => Object.values(marks).filter((value) => value === status).length

  const setMark = (id: string, status: MarkStatus) => {
    haptic()
    setMarks((current) => ({ ...current, [id]: status }))
  }

  return (
    <List>
      <Section header="Пара" footer="Данные о посещении в приложении не сохраняются — отметьте явку в чате с ботом.">
        <div className="flex gap-2 overflow-x-auto p-3" role="radiogroup" aria-label="Выбор пары">
          {lessons.length === 0 && <p className="px-1 py-2 text-sm text-tg-hint">Сегодня пар нет</p>}
          {lessons.map((item) => (
            <button
              key={item.para}
              type="button"
              role="radio"
              aria-checked={activePara === item.para}
              onClick={() => {
                haptic()
                setSelectedPara(item.para)
              }}
              className={cn(
                'flex shrink-0 flex-col items-start gap-0.5 rounded-xl px-3 py-2 text-left transition-colors',
                activePara === item.para ? 'bg-tg-button text-tg-button-text' : 'bg-tg-secondary-bg text-tg-text',
              )}
            >
              <span className="text-xs font-semibold">{item.para} пара</span>
              <span className="max-w-[160px] truncate text-xs opacity-80">{item.subject}</span>
            </button>
          ))}
        </div>
      </Section>

      {lesson && (
        <Section header={lesson.subject} footer={`${lesson.time}${lesson.room ? ` · каб. ${lesson.room}` : ''}`}>
          <div className="grid grid-cols-4 gap-1 p-3">
            {STATUSES.map((status) => (
              <span key={status} className="text-center text-xs font-medium text-tg-hint tabular-nums">
                {MARK_LABEL[status]}: {count(status)}
              </span>
            ))}
          </div>

          {members.map((member) => (
            <div key={member.id} className="flex items-center justify-between gap-2 border-b border-tg-divider px-4 py-2 last:border-b-0">
              <span className="truncate text-[15px]">{member.name}</span>
              <div className="flex shrink-0 gap-1" role="radiogroup" aria-label={`Отметка: ${member.name}`}>
                {STATUSES.map((status) => (
                  <button
                    key={status}
                    type="button"
                    role="radio"
                    aria-checked={marks[member.id] === status}
                    onClick={() => setMark(member.id, status)}
                    className={cn(
                      'rounded-lg px-2 py-1 text-xs font-semibold transition-colors',
                      marks[member.id] === status ? ACTIVE_TONE[status] : 'bg-tg-secondary-bg text-tg-hint',
                    )}
                  >
                    {MARK_LABEL[status]}
                  </button>
                ))}
              </div>
            </div>
          ))}

          <div className="px-1 pb-2">
            <Button
              stretched
              size="l"
              mode="bezeled"
              onClick={() => notify('Отметки в приложении не сохраняются. Отметьте явку командой /mark в чате с ботом.')}
            >
              Как сохранить
            </Button>
          </div>
        </Section>
      )}
    </List>
  )
}
