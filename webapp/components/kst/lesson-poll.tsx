'use client'

import { useEffect, useState } from 'react'
import { Button, Section } from '@telegram-apps/telegram-ui'
import { Check, Lock, Timer, X } from 'lucide-react'

import { apiPost, errorMessage } from '@/lib/api'
import type { ActiveLessonInfo } from '@/lib/api-types'
import { haptic, notify } from '@/lib/telegram'
import { cn } from '@/lib/utils'

/** Ответ бэкенда на отметку: {ok: true, status}. */
type AnswerPayload = { ok: true; status: 'present' | 'absent' }

/**
 * Опрос «Я на паре?» для идущей пары.
 *
 * Данные приходят пропсом из родителя (``/api/attendance/active``): опрос —
 * часть экрана «Явка», отдельный запрос дублировал бы данные. Таймер считается
 * от ``closes_at`` (начало пары + 5 минут), а не от локального счётчика: при
 * переоткрытии приложения остаток времени остаётся верным.
 *
 * Если ответа нет, а окно закрылось — бот уже проставил ``absent``
 * автозакрытием опроса, поэтому показываем это как итог.
 */
export function LessonPoll({
  active,
  loading,
  onAnswered,
}: {
  active: ActiveLessonInfo | null
  loading: boolean
  onAnswered: () => void
}) {
  const [now, setNow] = useState(() => Date.now())
  const [sending, setSending] = useState(false)
  const [answer, setAnswer] = useState<'present' | 'absent' | null>(null)
  const [error, setError] = useState<string | null>(null)

  const closesAt = active?.closes_at ? Date.parse(active.closes_at) : null
  const open = closesAt !== null && now <= closesAt

  // Тик раз в секунду, пока окно открыто: нужен для обратного отсчёта.
  useEffect(() => {
    if (closesAt === null || !open) return
    const id = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(id)
  }, [closesAt, open])

  // Сменилась пара — сбрасываем локальный ответ, чтобы показать актуальный.
  useEffect(() => {
    setAnswer(null)
    setError(null)
  }, [active?.para, active?.closes_at])

  if (loading && !active) {
    return (
      <Section header="Сейчас идёт пара">
        <div className="h-32 animate-pulse rounded-2xl bg-tg-secondary-bg" aria-busy="true" />
      </Section>
    )
  }

  if (!active) return null

  const answered = answer ?? active.answered
  const secondsLeft = closesAt ? Math.max(0, Math.round((closesAt - now) / 1000)) : 0
  const minutes = Math.floor(secondsLeft / 60)
  const seconds = String(secondsLeft % 60).padStart(2, '0')
  const progress = Math.min(100, (secondsLeft / 300) * 100)

  const submit = async (value: 'yes' | 'no') => {
    if (sending) return
    setSending(true)
    setError(null)
    try {
      const payload = await apiPost<AnswerPayload>('/api/attendance/answer', { answer: value })
      haptic()
      setAnswer(payload.status)
      onAnswered()
    } catch (cause) {
      setError(errorMessage(cause))
      notify(errorMessage(cause))
    } finally {
      setSending(false)
    }
  }

  return (
    <Section header={open ? 'Сейчас идёт пара' : 'Опрос закрыт'}>
      <div className="flex flex-col gap-4 p-4">
        <div className="min-w-0">
          <p className="text-xs font-semibold tracking-wide text-tg-hint uppercase">{active.para} пара</p>
          <p className="mt-0.5 text-lg font-semibold text-balance">{active.subject}</p>
          <p className="text-sm text-tg-hint tabular-nums">
            {active.time_range}
            {active.room ? ` · каб. ${active.room}` : ''}
          </p>
        </div>

        {answered !== null ? (
          <div
            className={cn(
              'flex items-center gap-3 rounded-xl px-3 py-3',
              answered === 'present' ? 'bg-tg-success/15 text-tg-success' : 'bg-tg-danger/15 text-tg-danger',
            )}
          >
            <Check className="size-5 shrink-0" aria-hidden="true" />
            <p className="text-sm font-medium">
              {answered === 'present' ? 'Вы отмечены как «на паре»' : 'Вы отмечены как «нет на паре»'}
            </p>
          </div>
        ) : open ? (
          <>
            <div className="flex flex-col gap-1.5">
              <div className="flex items-center justify-between text-sm">
                <span className="flex items-center gap-1.5 text-tg-hint">
                  <Timer className="size-4" aria-hidden="true" />
                  Осталось на ответ
                </span>
                <span className="font-semibold tabular-nums" aria-live="polite">
                  {minutes}:{seconds}
                </span>
              </div>
              <div className="h-1.5 overflow-hidden rounded-full bg-tg-secondary-bg">
                <div
                  className="h-full rounded-full bg-tg-link transition-[width] duration-1000 ease-linear"
                  style={{ width: `${progress}%` }}
                />
              </div>
            </div>

            <p className="text-[15px] font-medium">Вы на паре?</p>
            <div className="grid grid-cols-2 gap-2">
              <PollButton tone="success" disabled={sending} onClick={() => void submit('yes')}>
                <Check className="size-5" aria-hidden="true" />Я на паре
              </PollButton>
              <PollButton tone="destructive" disabled={sending} onClick={() => void submit('no')}>
                <X className="size-5" aria-hidden="true" />Меня нет
              </PollButton>
            </div>
            <p className="text-xs text-tg-hint">
              Если не ответить в течение 5 минут от начала пары — будет отмечен пропуск.
            </p>
          </>
        ) : (
          <div className="flex items-center gap-3 rounded-xl bg-tg-secondary-bg px-3 py-3 text-tg-hint">
            <Lock className="size-5 shrink-0" aria-hidden="true" />
            <p className="text-sm font-medium">Время ответа вышло — отметку поставит бот.</p>
          </div>
        )}

        {error && (
          <p className="text-xs text-tg-danger" role="alert">
            {error}
          </p>
        )}

        {!open && (
          <Button mode="plain" size="s" onClick={onAnswered}>
            Проверить статус
          </Button>
        )}
      </div>
    </Section>
  )
}

function PollButton({
  tone,
  disabled,
  onClick,
  children,
}: {
  tone: 'success' | 'destructive'
  disabled: boolean
  onClick: () => void
  children: React.ReactNode
}) {
  return (
    <button
      type="button"
      disabled={disabled}
      onClick={onClick}
      className={cn(
        'flex h-12 items-center justify-center gap-2 rounded-xl text-[15px] font-semibold transition-colors active:opacity-70 disabled:opacity-50',
        tone === 'success' ? 'bg-tg-success/15 text-tg-success' : 'bg-tg-danger/15 text-tg-danger',
      )}
    >
      {children}
    </button>
  )
}
