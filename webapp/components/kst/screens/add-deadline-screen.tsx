'use client'

import { useState, type FormEvent } from 'react'
import { Button, Input, List, Section } from '@telegram-apps/telegram-ui'
import { CalendarDays, GraduationCap } from 'lucide-react'

import { apiPost, errorMessage } from '@/lib/api'
import type { Deadline, DeadlineCreatePayload } from '@/lib/api-types'
import { addDaysISO, dateParts, relativeLabel, todayISO } from '@/lib/kst-format'
import { haptic } from '@/lib/telegram'

/**
 * Создание дедлайна: POST /api/deadlines.
 *
 * Предмет и преподаватель — свободный текст: бэкенд принимает любые строки
 * (в боте так же можно вписать задачу без привязки к расписанию). Дата
 * необязательна — без неё дедлайн попадёт в группу «Без срока».
 */
export function AddDeadlineScreen({ onCreated }: { onCreated: (created: Deadline) => void }) {
  const [task, setTask] = useState('')
  const [subject, setSubject] = useState('')
  const [teacher, setTeacher] = useState('')
  const [dateISO, setDateISO] = useState('')
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const trimmed = task.trim()
  const valid = trimmed.length > 0 && trimmed.length <= 120

  const handleSubmit = async (event: FormEvent) => {
    event.preventDefault()
    if (!valid || saving) return

    setSaving(true)
    setError(null)
    try {
      const payload: DeadlineCreatePayload = {
        task: trimmed,
        subject: subject.trim(),
        teacher: teacher.trim(),
        date: dateISO,
      }
      const response = await apiPost<{ ok: true; item: Deadline }>('/api/deadlines', payload)
      haptic()
      onCreated(response.item)
    } catch (cause) {
      // Ошибку показываем на экране, а не только во всплывающем окне: форма
      // остаётся заполненной, и пользователь может исправить поле.
      setError(errorMessage(cause))
    } finally {
      setSaving(false)
    }
  }

  const preview = dateISO ? dateParts(dateISO) : null

  return (
    <form onSubmit={handleSubmit}>
      <List>
        <Section header="Задача">
          <Input
            header="Название"
            placeholder="Например, лабораторная №4"
            value={task}
            maxLength={120}
            onChange={(event) => setTask(event.target.value)}
            required
          />
          <Input
            header="Предмет"
            placeholder="Математика (необязательно)"
            value={subject}
            maxLength={120}
            onChange={(event) => setSubject(event.target.value)}
          />
          <Input
            header="Преподаватель"
            placeholder="Соколова Е.В. (необязательно)"
            value={teacher}
            maxLength={120}
            onChange={(event) => setTeacher(event.target.value)}
          />
          {(subject || teacher) && (
            <div className="flex items-center gap-3 px-6 py-3">
              <span className="flex size-9 items-center justify-center rounded-full bg-tg-link/12 text-tg-link">
                <GraduationCap className="size-5" aria-hidden="true" />
              </span>
              <div className="flex flex-col">
                <span className="text-xs text-tg-hint">Будет сохранено как</span>
                <span className="text-[15px] font-medium">
                  {[subject.trim(), teacher.trim()].filter(Boolean).join(' · ')}
                </span>
              </div>
            </div>
          )}
        </Section>

<Section header="Срок сдачи" footer="Можно оставить пустым — дедлайн попадёт в группу «Без срока».">
          <div className="flex flex-col gap-4 px-4 py-4">
            <div className="flex items-center gap-2">
              <CalendarDays className="size-4 shrink-0 text-tg-hint" aria-hidden="true" />
              <input
                type="date"
                value={dateISO}
                min={todayISO()}
                max={addDaysISO(todayISO(), 365)}
                onChange={(event) => setDateISO(event.target.value)}
                aria-label="Дата сдачи"
                className="h-11 flex-1 rounded-xl border border-tg-divider bg-transparent px-3 text-[15px] text-tg-text"
              />
            </div>

            {dateISO && (
              <button
                type="button"
                onClick={() => setDateISO('')}
                className="self-start text-sm font-medium text-tg-link"
              >
                Убрать дату
              </button>
            )}

            {preview && (
              <div className="flex items-center gap-3 rounded-2xl bg-tg-link/10 p-3">
                <div className="flex w-12 shrink-0 flex-col items-center rounded-xl bg-tg-link py-1.5 text-white">
                  <span className="text-[10px] font-semibold uppercase opacity-80">{preview.weekdayShort}</span>
                  <span className="text-xl leading-none font-bold tabular-nums">{preview.day}</span>
                  <span className="text-[10px] font-medium">{preview.month}</span>
                </div>
                <div className="flex min-w-0 flex-col">
                  <span className="text-[15px] font-semibold">{relativeLabel(daysUntil(dateISO))}</span>
                  <span className="text-xs text-tg-subtitle first-letter:uppercase">{preview.long}</span>
                </div>
              </div>
            )}
          </div>
        </Section>

        {error && (
          <Section>
            <p className="px-4 py-3 text-sm text-tg-danger" role="alert">
              {error}
            </p>
          </Section>
        )}

        <div className="flex flex-col gap-2 px-1">
          <Button type="submit" stretched size="l" disabled={!valid || saving} loading={saving}>
            {saving ? 'Сохраняем…' : 'Сохранить'}
          </Button>
          {trimmed.length > 120 && (
            <p className="text-center text-xs text-tg-danger">Название длиннее 120 символов</p>
          )}
        </div>
      </List>
    </form>
  )
}

/** Сколько дней осталось до ISO-даты (0 — сегодня, отрицательное — просрочено). */
function daysUntil(iso: string): number {
  const today = new Date(`${todayISO()}T00:00:00Z`).getTime()
  const target = new Date(`${iso}T00:00:00Z`).getTime()
  return Math.round((target - today) / 86_400_000)
}
