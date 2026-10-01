'use client'

import { useEffect, useState } from 'react'
import { Button, Section } from '@telegram-apps/telegram-ui'
import { Check, Lock, MessagesSquare, Send, Timer, X } from 'lucide-react'
import { ACTIVE_LESSON, POLL_WINDOW_SECONDS, type AttendanceMode } from '@/lib/kst-data'
import { haptic } from '@/lib/telegram'
import { cn } from '@/lib/utils'

type Answer = 'present' | 'absent' | null

export function LessonPoll({ mode }: { mode: AttendanceMode }) {
  const [secondsLeft, setSecondsLeft] = useState(POLL_WINDOW_SECONDS)
  const [answer, setAnswer] = useState<Answer>(null)
  const closed = secondsLeft <= 0

  useEffect(() => {
    if (closed) return
    const id = setInterval(() => setSecondsLeft((s) => s - 1), 1000)
    return () => clearInterval(id)
  }, [closed])

  const finalAnswer: Answer = closed ? (answer ?? 'absent') : answer
  const minutes = Math.floor(Math.max(0, secondsLeft) / 60)
  const seconds = String(Math.max(0, secondsLeft) % 60).padStart(2, '0')
  const progress = (Math.max(0, secondsLeft) / POLL_WINDOW_SECONDS) * 100

  const choose = (value: Exclude<Answer, null>) => {
    haptic()
    setAnswer(value)
  }

  return (
    <Section header={closed ? 'Опрос закрыт' : 'Сейчас идёт пара'}>
      <div className="flex flex-col gap-4 p-4">
        <div className="flex items-start justify-between gap-3">
          <div className="min-w-0">
            <p className="text-xs font-semibold tracking-wide text-tg-hint uppercase">
              {ACTIVE_LESSON.code} · {ACTIVE_LESSON.number} пара
            </p>
            <p className="mt-0.5 text-lg font-semibold text-balance">{ACTIVE_LESSON.subject}</p>
            <p className="text-sm text-tg-hint tabular-nums">{ACTIVE_LESSON.time}</p>
          </div>
          <span className="flex shrink-0 items-center gap-1 rounded-full bg-tg-secondary-bg px-2.5 py-1 text-xs text-tg-hint">
            {mode === 'chat' ? (
              <MessagesSquare className="size-3.5" aria-hidden="true" />
            ) : (
              <Send className="size-3.5" aria-hidden="true" />
            )}
            {mode === 'chat' ? 'Чат группы' : 'Личка'}
          </span>
        </div>

        {closed ? (
          <div
            className={cn(
              'flex items-center gap-3 rounded-xl px-3 py-3',
              finalAnswer === 'present' ? 'bg-tg-success/15 text-tg-success' : 'bg-tg-destructive/15 text-tg-destructive',
            )}
          >
            <Lock className="size-5 shrink-0" aria-hidden="true" />
            <p className="text-sm font-medium">
              {finalAnswer === 'present'
                ? 'Ты: был на паре'
                : answer === null
                  ? 'Ты не ответил — отмечен как отсутствующий'
                  : 'Ты: не был на паре'}
            </p>
          </div>
        ) : (
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
                <div className="h-full rounded-full bg-tg-link transition-[width] duration-1000 ease-linear" style={{ width: `${progress}%` }} />
              </div>
            </div>

            <p className="text-[15px] font-medium">Ты на паре?</p>
            <div className="grid grid-cols-2 gap-2">
              <PollButton selected={answer === 'present'} tone="success" onClick={() => choose('present')}>
                <Check className="size-5" aria-hidden="true" />Я на паре
              </PollButton>
              <PollButton selected={answer === 'absent'} tone="destructive" onClick={() => choose('absent')}>
                <X className="size-5" aria-hidden="true" />Меня нет
              </PollButton>
            </div>
            <p className="text-xs text-tg-hint">
              {answer ? 'Ответ можно изменить, пока опрос открыт.' : 'Если не ответить за 5 минут — будет отмечен пропуск.'}
            </p>
          </>
        )}

        {closed && (
          <Button mode="plain" size="s" onClick={() => { setAnswer(null); setSecondsLeft(POLL_WINDOW_SECONDS) }}>
            Повторить демо-опрос
          </Button>
        )}
      </div>
    </Section>
  )
}

function PollButton({
  selected,
  tone,
  onClick,
  children,
}: {
  selected: boolean
  tone: 'success' | 'destructive'
  onClick: () => void
  children: React.ReactNode
}) {
  return (
    <button
      type="button"
      aria-pressed={selected}
      onClick={onClick}
      className={cn(
        'flex h-12 items-center justify-center gap-2 rounded-xl text-[15px] font-semibold transition-colors active:opacity-70',
        selected
          ? tone === 'success'
            ? 'bg-tg-success text-white'
            : 'bg-tg-destructive text-white'
          : tone === 'success'
            ? 'bg-tg-success/15 text-tg-success'
            : 'bg-tg-destructive/15 text-tg-destructive',
      )}
    >
      {children}
    </button>
  )
}
