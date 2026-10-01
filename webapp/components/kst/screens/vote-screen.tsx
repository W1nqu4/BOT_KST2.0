'use client'

import { useState } from 'react'
import { Button, List, Section } from '@telegram-apps/telegram-ui'
import { Check, CircleAlert, CircleCheck, Lock, RotateCcw } from 'lucide-react'
import { ACTIVE_LESSON, plural, type GroupMember } from '@/lib/kst-data'
import { haptic } from '@/lib/telegram'
import { cn } from '@/lib/utils'

const CANDIDATE_IDS = ['u4', 'u5', 'u7', 'u8']
const SEED_VOTES: Record<string, number> = { u4: 1, u5: 3, u7: 0, u8: 2 }

export function VoteScreen({ members }: { members: GroupMember[] }) {
  const candidates = members.filter((m) => CANDIDATE_IDS.includes(m.id))
  const threshold = Math.ceil(members.length / 2)
  const [myVotes, setMyVotes] = useState<Set<string>>(new Set())
  const [closed, setClosed] = useState(false)

  const votesFor = (id: string) => (SEED_VOTES[id] ?? 0) + (myVotes.has(id) ? 1 : 0)
  const passed = candidates.filter((c) => votesFor(c.id) >= threshold)
  const failed = candidates.filter((c) => votesFor(c.id) < threshold)

  const toggle = (id: string) => {
    haptic()
    setMyVotes((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  return (
    <List>
      <Section>
        <div className="flex flex-col gap-1 p-4">
          <p className="text-xs font-semibold tracking-wide text-tg-hint uppercase">
            {ACTIVE_LESSON.number} пара · {ACTIVE_LESSON.date}
          </p>
          <p className="text-lg font-semibold">{ACTIVE_LESSON.subject}</p>
          <p className="text-sm text-tg-hint">
            Порог зачёта: <span className="font-semibold text-tg-text">{threshold} {plural(threshold, 'голос', 'голоса', 'голосов')}</span> из {members.length}
          </p>
        </div>
      </Section>

      {closed ? (
        <>
          <Section header="Зачтены">
            {passed.length ? (
              passed.map((c) => (
                <ResultRow key={c.id} name={c.name} votes={votesFor(c.id)} ok />
              ))
            ) : (
              <p className="px-4 py-3 text-sm text-tg-hint">Никто не набрал порог</p>
            )}
          </Section>
          <Section header="Не набрали" footer="Староста, проверь этих студентов вручную в разделе «Отметить вручную».">
            {failed.length ? (
              failed.map((c) => <ResultRow key={c.id} name={c.name} votes={votesFor(c.id)} ok={false} />)
            ) : (
              <p className="px-4 py-3 text-sm text-tg-hint">Все набрали порог</p>
            )}
          </Section>
          <div className="px-1">
            <Button
              stretched
              size="l"
              mode="bezeled"
              before={<RotateCcw className="size-5" aria-hidden="true" />}
              onClick={() => {
                setClosed(false)
                setMyVotes(new Set())
              }}
            >
              Новое голосование
            </Button>
          </div>
        </>
      ) : (
        <>
          <Section header="Кто был на паре?" footer="Нажми на фамилию, чтобы подтвердить. Можно голосовать и за себя.">
            <div className="flex flex-col gap-2 p-3">
              {candidates.map((c) => {
                const votes = votesFor(c.id)
                const reached = votes >= threshold
                const mine = myVotes.has(c.id)
                return (
                  <button
                    key={c.id}
                    type="button"
                    aria-pressed={mine}
                    onClick={() => toggle(c.id)}
                    className={cn(
                      'relative flex items-center gap-3 overflow-hidden rounded-xl px-3 py-3 text-left transition-colors active:opacity-70',
                      mine ? 'bg-tg-link/15 ring-1 ring-tg-link' : 'bg-tg-secondary-bg',
                    )}
                  >
                    <span
                      className={cn(
                        'flex size-6 shrink-0 items-center justify-center rounded-full border-2',
                        mine ? 'border-tg-link bg-tg-link text-white' : 'border-tg-hint/50',
                      )}
                    >
                      {mine && <Check className="size-4" aria-hidden="true" />}
                    </span>
                    <span className="flex min-w-0 flex-1 flex-col gap-1.5">
                      <span className="truncate text-[15px] font-medium">{c.name}</span>
                      <span className="block h-1 overflow-hidden rounded-full bg-tg-bg">
                        <span
                          className={cn('block h-full rounded-full transition-all', reached ? 'bg-tg-success' : 'bg-tg-link')}
                          style={{ width: `${Math.min(100, (votes / threshold) * 100)}%` }}
                        />
                      </span>
                    </span>
                    <span className={cn('flex shrink-0 items-center gap-1 text-sm font-semibold tabular-nums', reached && 'text-tg-success')}>
                      {votes}/{threshold}
                      {reached && <CircleCheck className="size-4" aria-label="Порог набран" />}
                    </span>
                  </button>
                )
              })}
            </div>
          </Section>
          <div className="px-1">
            <Button
              stretched
              size="l"
              mode="bezeled"
              before={<Lock className="size-5" aria-hidden="true" />}
              onClick={() => {
                haptic()
                setClosed(true)
              }}
            >
              Закрыть досрочно
            </Button>
          </div>
        </>
      )}
    </List>
  )
}

function ResultRow({ name, votes, ok }: { name: string; votes: number; ok: boolean }) {
  const Icon = ok ? CircleCheck : CircleAlert
  return (
    <div className="flex items-center gap-3 border-b border-tg-divider px-4 py-3 last:border-b-0">
      <Icon className={cn('size-5', ok ? 'text-tg-success' : 'text-tg-warning')} aria-hidden="true" />
      <span className="flex-1 text-[15px]">{name}</span>
      <span className="text-sm text-tg-hint tabular-nums">
        {votes} {plural(votes, 'голос', 'голоса', 'голосов')}
      </span>
    </div>
  )
}
