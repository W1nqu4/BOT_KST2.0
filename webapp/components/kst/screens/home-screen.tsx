'use client'

import { useState } from 'react'
import { Clock, MapPin, UserRound } from 'lucide-react'

import type { Lesson, ScheduleDay } from '@/lib/api-types'
import { useApi } from '@/lib/use-api'
import { haptic } from '@/lib/telegram'
import { dateParts, todayISO, weekdaysFrom } from '@/lib/kst-format'
import { cn } from '@/lib/utils'
import { LessonStatusChip, LessonStatusIcon } from '../lesson-status'
import { ErrorState, SkeletonHero } from '../async-state'

/**
 * Главный экран: расписание на сегодня и переключение по дням недели.
 *
 * Данные: ``/api/schedule/day`` с датой выбранного дня; по умолчанию — сегодня.
 * Так один хук обслуживает и «сегодня», и клик по дню недели: формат ответа у
 * ``/today`` и ``/day`` одинаковый, отдельный запрос не нужен.
 */
export function HomeScreen({ onNeedGroup }: { onNeedGroup?: () => void }) {
  const [selected, setSelected] = useState<string | null>(null)

  const today = todayISO()
  const displayed = selected ?? today
  const { data, loading, error, errorCode, refresh } = useApi<ScheduleDay>(
    `/api/schedule/day?date=${displayed}`,
  )

  if (errorCode === 'group_not_set') {
    return (
      <ErrorState
        message="Чтобы показывать расписание, укажите группу в чате с ботом."
        action={onNeedGroup ? { label: 'Как выбрать группу', onClick: onNeedGroup } : undefined}
      />
    )
  }
  if (loading && !data) return <SkeletonHero />
  if (error && !data) return <ErrorState message={error} onRetry={refresh} />

  const lessons = data?.lessons ?? []
  const isToday = displayed === today
  const parts = dateParts(displayed)
  const active = lessons.filter((lesson) => !lesson.is_cancelled).length
  const replaced = lessons.filter((lesson) => lesson.status === 'substituted').length
  const cancelled = lessons.filter((lesson) => lesson.status === 'cancelled').length

  return (
    <div className="flex flex-col gap-4 pt-4">
      <section
        className="relative mx-4 overflow-hidden rounded-3xl bg-tg-button p-5 text-tg-button-text shadow-lg shadow-tg-button/20"
        aria-label="Сводка дня"
      >
        <div className="pointer-events-none absolute -top-12 -right-10 size-40 rounded-full bg-white/10" aria-hidden="true" />
        <div className="pointer-events-none absolute -bottom-16 -left-6 size-32 rounded-full bg-white/5" aria-hidden="true" />

        <div className="relative flex items-center justify-between">
          <span className="text-xs font-medium tracking-[0.14em] uppercase opacity-75">
            {isToday ? 'Сегодня' : 'Выбранный день'}
          </span>
          <span className="rounded-full bg-white/20 px-3 py-1 text-xs font-bold tracking-wide">
            {data?.group || '—'}
          </span>
        </div>

        <div className="relative mt-4 flex items-center gap-4">
          <div className="flex size-[72px] shrink-0 flex-col items-center justify-center rounded-2xl bg-white/20 backdrop-blur-sm">
            <span className="text-[32px] leading-none font-bold tabular-nums">{parts.day}</span>
            <span className="mt-1 text-[11px] font-semibold tracking-wider uppercase opacity-80">{parts.month}</span>
          </div>
          <div className="flex min-w-0 flex-col gap-1.5">
            <p className="text-2xl leading-tight font-bold">{data?.weekday || parts.long}</p>
            <span className="inline-flex w-fit items-center gap-1.5 rounded-full bg-white px-2.5 py-0.5 text-xs font-semibold text-tg-button">
              <span className="size-1.5 rounded-full bg-tg-button" aria-hidden="true" />
              {weekTypeLabel(data?.week_type)}
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
        {weekdaysFrom(displayed).map((item) => {
          const isSelected = item.iso === displayed
          return (
            <button
              key={item.iso}
              type="button"
              aria-pressed={isSelected}
              onClick={() => {
                haptic()
                setSelected(item.iso === today ? null : item.iso)
              }}
              className={cn(
                'flex flex-col items-center gap-0.5 rounded-2xl py-2 transition-colors',
                isSelected ? 'bg-tg-button text-tg-button-text' : 'bg-tg-section text-tg-text',
              )}
            >
              <span
                className={cn(
                  'text-[11px] font-medium uppercase',
                  isSelected ? 'opacity-80' : item.iso === today ? 'text-tg-link' : 'text-tg-hint',
                )}
              >
                {item.short}
              </span>
              <span className="text-lg leading-none font-bold tabular-nums">{item.day}</span>
              <span className={cn('text-[10px] font-medium', isSelected ? 'opacity-80' : 'text-tg-hint')}>
                {item.day % 2 === 1 ? 'нечёт' : 'чёт'}
              </span>
            </button>
          )
        })}
      </nav>

      <div key={displayed} className="flex animate-in flex-col gap-3 px-4 duration-300 fade-in slide-in-from-bottom-2">
        {lessons.length === 0 ? (
          <p className="rounded-2xl bg-tg-section px-4 py-6 text-center text-sm text-tg-hint">
            Пар нет — можно заняться дедлайнами
          </p>
        ) : (
          lessons.map((lesson) => <LessonCard key={lesson.para} lesson={lesson} />)
        )}
      </div>
    </div>
  )
}
/** Чётность из API («Чет»/«нечет») → человеческая подпись. */
function weekTypeLabel(weekType?: string) {
  if (!weekType) return '—'
  return weekType.toLowerCase().startsWith('неч') ? 'Нечётный день' : 'Чётный день'
}

function HeroStat({ label, value }: { label: string; value: number }) {
  return (
    <div className="flex flex-col rounded-xl bg-white/15 px-3 py-2">
      <dt className="text-[11px] font-medium opacity-75">{label}</dt>
      <dd className="text-lg leading-tight font-bold tabular-nums">{value}</dd>
    </div>
  )
}

function LessonCard({ lesson }: { lesson: Lesson }) {
  return (
    <article
      className={cn(
        'flex gap-3 rounded-2xl bg-tg-section p-4 shadow-[0_1px_2px_rgba(0,0,0,0.06)]',
        lesson.is_cancelled && 'opacity-60',
      )}
    >
      <LessonStatusIcon status={lesson.status} />

      <div className="flex min-w-0 flex-1 flex-col gap-1">
        <div className="flex items-center justify-between gap-2">
          <span className="text-xs font-semibold tracking-wide text-tg-hint uppercase">{lesson.para} пара</span>
          {lesson.time && (
            <span className="flex items-center gap-1 text-xs font-medium text-tg-hint tabular-nums">
              <Clock className="size-3.5" aria-hidden="true" />
              {lesson.time}
            </span>
          )}
        </div>

        <h3 className={cn('text-[17px] leading-snug font-semibold', lesson.is_cancelled && 'line-through')}>
          {lesson.subject}
        </h3>

        <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-sm text-tg-subtitle">
          {lesson.teacher && (
            <span className="flex items-center gap-1">
              <UserRound className="size-3.5" aria-hidden="true" />
              {lesson.teacher}
            </span>
          )}
          {lesson.room && (
            <span className="flex items-center gap-1">
              <MapPin className="size-3.5" aria-hidden="true" />
              Каб. {lesson.room}
            </span>
          )}
        </div>

        {lesson.status !== 'planned' && (
          <div className="mt-1 flex flex-wrap items-center gap-2">
            <LessonStatusChip status={lesson.status} />
            {lesson.old_subject && <span className="text-xs text-tg-hint">Вместо «{lesson.old_subject}»</span>}
          </div>
        )}
      </div>
    </article>
  )
}