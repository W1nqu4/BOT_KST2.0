'use client'

import { useState } from 'react'
import { Placeholder } from '@telegram-apps/telegram-ui'
import { Clock, MapPin, UserRound } from 'lucide-react'
import { DAYS, LESSON_TIMES, SCHEDULE, STUDENT, TODAY_KEY, type DayKey, type Lesson } from '@/lib/kst-data'
import { haptic } from '@/lib/telegram'
import { cn } from '@/lib/utils'
import { LessonStatusChip, LessonStatusIcon } from '../lesson-status'

export function HomeScreen() {
  const [selectedDay, setSelectedDay] = useState<DayKey>(TODAY_KEY)
  const day = DAYS.find((d) => d.key === selectedDay)!
  const lessons = SCHEDULE[selectedDay]
  const isToday = selectedDay === TODAY_KEY

  const [dayNumber, monthName] = day.date.split(' ')
  const isOddDay = Number(dayNumber) % 2 === 1
  const active = lessons.filter((l) => l.status !== 'cancel').length
  const replaced = lessons.filter((l) => l.status === 'replace').length
  const cancelled = lessons.filter((l) => l.status === 'cancel').length

  return (
    <div className="flex flex-col gap-4 pt-4">
      <section
        className="relative mx-4 overflow-hidden rounded-3xl bg-tg-button p-5 text-tg-button-text shadow-lg shadow-tg-button/20"
        aria-label="Сводка дня"
      >
        <div className="pointer-events-none absolute -right-10 -top-12 size-40 rounded-full bg-white/10" aria-hidden="true" />
        <div className="pointer-events-none absolute -bottom-16 -left-6 size-32 rounded-full bg-white/5" aria-hidden="true" />

        <div className="relative flex items-center justify-between">
          <span className="text-xs font-medium uppercase tracking-[0.14em] opacity-75">
            {isToday ? 'Сегодня' : 'Выбранный день'}
          </span>
          <span className="rounded-full bg-white/20 px-3 py-1 text-xs font-bold tracking-wide">{STUDENT.group}</span>
        </div>

        <div className="relative mt-4 flex items-center gap-4">
          <div className="flex size-[72px] shrink-0 flex-col items-center justify-center rounded-2xl bg-white/20 backdrop-blur-sm">
            <span className="text-[32px] font-bold leading-none tabular-nums">{dayNumber}</span>
            <span className="mt-1 text-[11px] font-semibold uppercase tracking-wider opacity-80">
              {monthName.slice(0, 3)}
            </span>
          </div>
          <div className="flex min-w-0 flex-col gap-1.5">
            <p className="text-2xl font-bold leading-tight">{day.full}</p>
            <span className="inline-flex w-fit items-center gap-1.5 rounded-full bg-white px-2.5 py-0.5 text-xs font-semibold text-tg-button">
              <span className="size-1.5 rounded-full bg-tg-button" aria-hidden="true" />
              {isOddDay ? 'Нечётный день' : 'Чётный день'}
            </span>
          </div>
        </div>

        <dl className="relative mt-5 grid grid-cols-3 gap-2">
          <HeroStat label="Пар" value={active} />
          <HeroStat label="Замены" value={replaced} />
          <HeroStat label="Отмены" value={cancelled} />
        </dl>
      </section>

      <nav aria-label="Дни недели" className="mx-4 grid grid-cols-6 gap-1.5">
        {DAYS.map((d) => {
          const num = d.date.split(' ')[0]
          const selected = d.key === selectedDay
          const odd = Number(num) % 2 === 1
          return (
            <button
              key={d.key}
              type="button"
              aria-pressed={selected}
              onClick={() => {
                haptic()
                setSelectedDay(d.key)
              }}
              className={cn(
                'flex flex-col items-center gap-0.5 rounded-2xl py-2 transition-colors',
                selected ? 'bg-tg-button text-tg-button-text' : 'bg-tg-section text-tg-text',
              )}
            >
              <span
                className={cn(
                  'text-[11px] font-medium uppercase',
                  selected ? 'opacity-80' : d.key === TODAY_KEY ? 'text-tg-link' : 'text-tg-hint',
                )}
              >
                {d.short}
              </span>
              <span className="text-lg font-bold leading-none tabular-nums">{num}</span>
              <span className={cn('text-[10px] font-medium', selected ? 'opacity-80' : 'text-tg-hint')}>
                {odd ? 'нечёт' : 'чёт'}
              </span>
            </button>
          )
        })}
      </nav>

      <div key={selectedDay} className="flex flex-col gap-3 px-4 animate-in fade-in slide-in-from-bottom-2 duration-300">
        {lessons.length === 0 ? (
          <Placeholder header="Пар нет" description="Можно отдохнуть или заняться дедлайнами" />
        ) : (
          lessons.map((lesson) => <LessonCard key={lesson.number} lesson={lesson} />)
        )}
      </div>
    </div>
  )
}

function HeroStat({ label, value }: { label: string; value: number }) {
  return (
    <div className="flex flex-col rounded-xl bg-white/15 px-3 py-2">
      <dt className="text-[11px] font-medium opacity-75">{label}</dt>
      <dd className="text-lg font-bold leading-tight tabular-nums">{value}</dd>
    </div>
  )
}

function LessonCard({ lesson }: { lesson: Lesson }) {
  const cancelled = lesson.status === 'cancel'
  return (
    <article
      className={cn(
        'flex gap-3 rounded-2xl bg-tg-section p-4 shadow-[0_1px_2px_rgba(0,0,0,0.06)]',
        cancelled && 'opacity-60',
      )}
    >
      <LessonStatusIcon status={lesson.status} />
      <div className="flex min-w-0 flex-1 flex-col gap-1">
        <div className="flex items-center justify-between gap-2">
          <span className="text-xs font-semibold uppercase tracking-wide text-tg-hint">{lesson.number} пара</span>
          <span className="flex items-center gap-1 text-xs font-medium text-tg-hint tabular-nums">
            <Clock className="size-3.5" aria-hidden="true" />
            {LESSON_TIMES[lesson.number]}
          </span>
        </div>
        <h3 className={cn('text-[17px] font-semibold leading-snug', cancelled && 'line-through')}>{lesson.subject}</h3>
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-sm text-tg-subtitle">
          <span className="flex items-center gap-1">
            <UserRound className="size-3.5" aria-hidden="true" />
            {lesson.teacher}
          </span>
          <span className="flex items-center gap-1">
            <MapPin className="size-3.5" aria-hidden="true" />
            Каб. {lesson.room}
          </span>
        </div>
        {lesson.status !== 'plan' && (
          <div className="mt-1 flex flex-wrap items-center gap-2">
            <LessonStatusChip status={lesson.status} />
            {lesson.note && <span className="text-xs text-tg-hint">{lesson.note}</span>}
          </div>
        )}
      </div>
    </article>
  )
}
