'use client'

import { useState } from 'react'
import { Button, Cell, List, Section } from '@telegram-apps/telegram-ui'
import {
  AlarmClock,
  CalendarDays,
  CircleAlert,
  CircleCheck,
  CircleX,
  FileText,
  type LucideIcon,
} from 'lucide-react'

import type { ActiveLessonPayload, AttendancePayload } from '@/lib/api-types'
import { useApi } from '@/lib/use-api'
import { cn } from '@/lib/utils'
import { LessonPoll } from '../lesson-poll'
import { ErrorState, SkeletonHero } from '../async-state'

/**
 * Экран «Явка»: сводка за месяц, аттестация по предметам и опрос текущей пары.
 *
 * Данные: ``/api/attendance`` (сводка + аттестация) и
 * ``/api/attendance/active`` (идёт ли пара). Процент считает бэкенд — правило
 * «зачитываются только «был» и «опоздал»» на фронте не дублируется. Порог
 * аттестации (``required``) и число прошедших пар (``total_lessons``) — разные
 * числа, поэтому показываются отдельно.
 */
export function AttendanceScreen({ onOpen }: { onOpen: (screen: 'mark' | 'vote' | 'report' | 'group') => void }) {
  const summary = useApi<AttendancePayload>('/api/attendance')
  const active = useApi<ActiveLessonPayload>('/api/attendance/active')
  const [showAll, setShowAll] = useState(false)

  if (summary.loading && !summary.data) return <SkeletonHero />
  if (summary.error && !summary.data) {
    if (summary.errorCode === 'not_in_group') {
      return <ErrorState message="Вы ещё не вступили в группу. Введите код старосты в чате с ботом." />
    }
    return <ErrorState message={summary.error} onRetry={summary.refresh} />
  }

  const data = summary.data
  const total = data?.total ?? 0
  const subjects = data?.subjects ?? []
  const visible = showAll ? subjects : subjects.slice(0, 5)
  const required = subjects[0]?.required ?? 3

  return (
    <List>
      <LessonPoll
        active={active.data?.active ?? null}
        loading={active.loading}
        onAnswered={() => {
          active.refresh()
          summary.refresh()
        }}
      />

      <Section header="Моя посещаемость · текущий месяц">
        <div className="flex flex-col gap-4 p-4">
          <div className="flex items-end justify-between">
            <div>
              <p className="text-sm text-tg-hint">Зачтено занятий</p>
              <p className="text-3xl font-bold tabular-nums">{data?.percent ?? 0}%</p>
            </div>
            <p className="text-sm text-tg-hint tabular-nums">{total} занятий</p>
          </div>

          {total > 0 && (
            <div
              className="flex h-2.5 overflow-hidden rounded-full bg-tg-secondary-bg"
              role="img"
              aria-label="Распределение посещаемости"
            >
              <span className="bg-tg-success" style={{ width: `${((data?.present ?? 0) / total) * 100}%` }} />
              <span className="bg-tg-warning" style={{ width: `${((data?.late ?? 0) / total) * 100}%` }} />
              <span className="bg-tg-link" style={{ width: `${((data?.excused ?? 0) / total) * 100}%` }} />
              <span className="bg-tg-danger" style={{ width: `${((data?.absent ?? 0) / total) * 100}%` }} />
            </div>
          )}

          <div className="grid grid-cols-2 gap-2">
            <Stat icon={CircleCheck} label="Присутствовал" value={data?.present ?? 0} className="text-tg-success" />
            <Stat icon={AlarmClock} label="Опоздал" value={data?.late ?? 0} className="text-tg-warning" />
            <Stat icon={CircleX} label="Пропустил" value={data?.absent ?? 0} className="text-tg-danger" />
            <Stat icon={FileText} label="По уважительной" value={data?.excused ?? 0} className="text-tg-link" />
          </div>

          {total === 0 && (
            <p className="text-sm text-tg-hint">
              Отметок пока нет. Ответьте на опрос во время пары — здесь появится статистика.
            </p>
          )}
        </div>
      </Section>

      <Section
        header="Аттестация по предметам"
        footer={`Минимум ${required} пары по предмету за месяц. Засчитываются только «был» и «опоздал».`}
      >
        {visible.length === 0 && (
          <p className="px-4 py-3 text-sm text-tg-hint">Предметы появятся после загрузки расписания.</p>
        )}
        {visible.map((subject) => {
          const passed = subject.is_attested
          const partial = !passed && subject.attended > 0
          const Icon = passed ? CircleCheck : partial ? CircleAlert : CircleX
          const tone = passed ? 'text-tg-success' : partial ? 'text-tg-warning' : 'text-tg-danger'
          const bar = passed ? 'bg-tg-success' : partial ? 'bg-tg-warning' : 'bg-tg-danger'
          const percent = Math.min(100, (subject.attended / Math.max(1, subject.required)) * 100)

          return (
            <Cell
              key={subject.name}
              multiline
              before={<Icon className={cn('size-6', tone)} aria-hidden="true" />}
              after={
                <span className={cn('text-[15px] font-semibold tabular-nums', tone)}>
                  {subject.attended}/{subject.required}
                </span>
              }
              description={
                <span className="flex flex-col gap-1">
                  <span className="mt-1.5 block h-1.5 w-full overflow-hidden rounded-full bg-tg-secondary-bg">
                    <span className={cn('block h-full rounded-full', bar)} style={{ width: `${percent}%` }} />
                  </span>
                  <span className="flex flex-wrap gap-x-3 text-xs">
                    {!passed && <span className={tone}>Нужно ещё {subject.need_more}</span>}
                    {subject.total_lessons > 0 && (
                      <span className="text-tg-hint">Пар по предмету: {subject.total_lessons}</span>
                    )}
                  </span>
                </span>
              }
            >
              {subject.name}
            </Cell>
          )
        })}

        {subjects.length > 5 && (
          <Cell Component="div" onClick={() => setShowAll((value) => !value)}>
            <span className="text-tg-link">
              {showAll ? 'Свернуть список' : `Показать все предметы (${subjects.length})`}
            </span>
          </Cell>
        )}
      </Section>

      <Section header="Управление явкой">
        <div className="px-1 pb-2">
          <Button
            stretched
            size="l"
            mode="bezeled"
            before={<CalendarDays className="size-5" aria-hidden="true" />}
            onClick={() => onOpen('report')}
          >
            Отчёт за неделю
          </Button>
        </div>
        <Cell Component="div" onClick={() => onOpen('mark')}>
          <span className="text-tg-link">Отметить вручную</span>
        </Cell>
        <Cell Component="div" onClick={() => onOpen('vote')}>
          <span className="text-tg-link">Голосование по прогулу</span>
        </Cell>
        <Cell Component="div" onClick={() => onOpen('group')}>
          <span className="text-tg-link">Моя группа</span>
        </Cell>
      </Section>
    </List>
  )
}

function Stat({
  icon: Icon,
  label,
  value,
  className,
}: {
  icon: LucideIcon
  label: string
  value: number
  className: string
}) {
  return (
    <div className="flex items-center gap-3 rounded-xl bg-tg-secondary-bg px-3 py-2.5">
      <Icon className={cn('size-5 shrink-0', className)} aria-hidden="true" />
      <div className="flex min-w-0 flex-col">
        <span className="text-lg leading-tight font-bold tabular-nums">{value}</span>
        <span className="truncate text-xs text-tg-hint">{label}</span>
      </div>
    </div>
  )
}