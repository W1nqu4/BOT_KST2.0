'use client'

import { useState, type FormEvent } from 'react'
import { Button, Input, List, Section, Select } from '@telegram-apps/telegram-ui'
import { CalendarDays, GraduationCap, Hourglass } from 'lucide-react'
import {
  SUBJECTS,
  TODAY_ISO,
  addDaysISO,
  dateParts,
  findLessonOccurrence,
  lessonsLabel,
  relativeLabel,
  teacherFor,
  type Deadline,
  type DueMode,
} from '@/lib/kst-data'
import { haptic } from '@/lib/telegram'
import { cn } from '@/lib/utils'

const LESSON_OPTIONS = [1, 2, 3, 4, 5]

export function AddDeadlineScreen({ onSubmit }: { onSubmit: (deadline: Deadline) => void }) {
  const [title, setTitle] = useState('')
  const [subject, setSubject] = useState(SUBJECTS[0])
  const [mode, setMode] = useState<DueMode>('lessons')
  const [lessonsAhead, setLessonsAhead] = useState(1)
  const [dateISO, setDateISO] = useState(addDaysISO(TODAY_ISO, 1))

  const teacher = teacherFor(subject)
  const occurrence = mode === 'lessons' ? findLessonOccurrence(subject, lessonsAhead) : null
  const dueISO = mode === 'lessons' ? occurrence?.iso : dateISO

  const trimmed = title.trim()
  const valid = trimmed.length > 0 && trimmed.length <= 120 && Boolean(dueISO)

  const handleSubmit = (e: FormEvent) => {
    e.preventDefault()
    if (!valid || !dueISO) return
    onSubmit({
      id: crypto.randomUUID(),
      title: trimmed,
      subject,
      teacher,
      dueMode: mode,
      dueLessons: mode === 'lessons' ? lessonsAhead : undefined,
      dueLessonNumber: occurrence?.lessonNumber,
      dueISO,
    })
  }

  const preview = dueISO ? dateParts(dueISO) : null

  return (
    <form onSubmit={handleSubmit}>
      <List>
        <Section header="Задача">
          <Input
            header="Название"
            placeholder="Например, лабораторная №4"
            value={title}
            maxLength={120}
            onChange={(e) => setTitle(e.target.value)}
            required
          />
          <Select header="Предмет" value={subject} onChange={(e) => setSubject(e.target.value)}>
            {SUBJECTS.map((s) => (
              <option key={s} value={s}>
                {s}
              </option>
            ))}
          </Select>
          <div className="flex items-center gap-3 px-6 py-3">
            <span className="flex size-9 items-center justify-center rounded-full bg-tg-link/12 text-tg-link">
              <GraduationCap className="size-5" aria-hidden="true" />
            </span>
            <div className="flex flex-col">
              <span className="text-xs text-tg-hint">Преподаватель</span>
              <span className="text-[15px] font-medium">{teacher}</span>
            </div>
          </div>
        </Section>

        <Section header="Срок сдачи">
          <div className="flex flex-col gap-4 px-4 py-4">
            <div role="radiogroup" aria-label="Тип срока" className="grid grid-cols-2 gap-1 rounded-xl bg-tg-fill p-1">
              {(
                [
                  { key: 'lessons', label: 'К паре', icon: Hourglass },
                  { key: 'date', label: 'К дате', icon: CalendarDays },
                ] as const
              ).map(({ key, label, icon: Icon }) => (
                <button
                  key={key}
                  type="button"
                  role="radio"
                  aria-checked={mode === key}
                  onClick={() => {
                    haptic()
                    setMode(key)
                  }}
                  className={cn(
                    'flex items-center justify-center gap-1.5 rounded-lg py-2 text-sm font-semibold transition-colors',
                    mode === key ? 'bg-tg-section text-tg-text shadow-sm' : 'text-tg-hint',
                  )}
                >
                  <Icon className="size-4" aria-hidden="true" />
                  {label}
                </button>
              ))}
            </div>

            {mode === 'lessons' ? (
              <div className="flex flex-wrap gap-2" role="radiogroup" aria-label="Через сколько занятий">
                {LESSON_OPTIONS.map((n) => (
                  <button
                    key={n}
                    type="button"
                    role="radio"
                    aria-checked={lessonsAhead === n}
                    onClick={() => {
                      haptic()
                      setLessonsAhead(n)
                    }}
                    className={cn(
                      'rounded-full border px-3 py-1.5 text-sm font-medium transition-colors',
                      lessonsAhead === n
                        ? 'border-tg-button bg-tg-button text-tg-button-text'
                        : 'border-tg-divider text-tg-text',
                    )}
                  >
                    {n === 1 ? 'След. пара' : `Через ${n}`}
                  </button>
                ))}
              </div>
            ) : (
              <input
                type="date"
                value={dateISO}
                min={TODAY_ISO}
                onChange={(e) => setDateISO(e.target.value)}
                aria-label="Дата сдачи"
                required
                className="h-11 rounded-xl border border-tg-divider bg-transparent px-3 text-[15px] text-tg-text"
              />
            )}

            {preview && dueISO && (
              <div className="flex items-center gap-3 rounded-2xl bg-tg-link/10 p-3">
                <div className="flex w-12 shrink-0 flex-col items-center rounded-xl bg-tg-link py-1.5 text-white">
                  <span className="text-[10px] font-semibold uppercase opacity-80">{preview.weekday}</span>
                  <span className="text-xl font-bold leading-none tabular-nums">{preview.day}</span>
                  <span className="text-[10px] font-medium">{preview.month}</span>
                </div>
                <div className="flex min-w-0 flex-col">
                  <span className="text-[15px] font-semibold">
                    {mode === 'lessons' ? lessonsLabel(lessonsAhead) : relativeLabel(dueISO)}
                  </span>
                  <span className="text-xs text-tg-subtitle first-letter:uppercase">
                    {preview.long}
                    {occurrence ? ` · ${occurrence.lessonNumber} пара` : ''}
                  </span>
                </div>
              </div>
            )}
          </div>
        </Section>

        <div className="px-1">
          <Button type="submit" stretched size="l" disabled={!valid}>
            Сохранить
          </Button>
        </div>
      </List>
    </form>
  )
}
