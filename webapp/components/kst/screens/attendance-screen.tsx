'use client'

import { useState } from 'react'
import { Button, Cell, List, Section } from '@telegram-apps/telegram-ui'
import {
  AlarmClock,
  CalendarDays,
  ChartNoAxesColumn,
  ChevronRight,
  CircleAlert,
  CircleCheck,
  CircleX,
  FileText,
  PencilLine,
  Settings2,
  Users,
  Vote,
  type LucideIcon,
} from 'lucide-react'
import { ATTENDANCE, permissions, type AttendanceMode, type Role } from '@/lib/kst-data'
import { cn } from '@/lib/utils'
import { LessonPoll } from '../lesson-poll'

type Props = {
  role: Role
  mode: AttendanceMode
  onOpen: (screen: 'mark' | 'vote' | 'report' | 'group') => void
}

export function AttendanceScreen({ role, mode, onOpen }: Props) {
  const [showPrevious, setShowPrevious] = useState(false)
  const month = showPrevious ? ATTENDANCE.previous : ATTENDANCE.current
  const total = month.present + month.late + month.absent + month.excused
  const rate = Math.round(((month.present + month.late) / total) * 100)
  const can = permissions(role)

  return (
    <List>
      {!showPrevious && <LessonPoll mode={mode} />}

      <Section header={`Моя посещаемость · ${month.label}`}>
        <div key={month.label} className="flex flex-col gap-4 p-4 animate-in fade-in duration-300">
          <div className="flex items-end justify-between">
            <div>
              <p className="text-sm text-tg-hint">Зачтено занятий</p>
              <p className="text-3xl font-bold tabular-nums">{rate}%</p>
            </div>
            <p className="text-sm text-tg-hint tabular-nums">{total} занятий</p>
          </div>
          <div className="flex h-2.5 overflow-hidden rounded-full bg-tg-secondary-bg" role="img" aria-label="Распределение посещаемости">
            <span className="bg-tg-success" style={{ width: `${(month.present / total) * 100}%` }} />
            <span className="bg-tg-warning" style={{ width: `${(month.late / total) * 100}%` }} />
            <span className="bg-tg-link" style={{ width: `${(month.excused / total) * 100}%` }} />
            <span className="bg-tg-destructive" style={{ width: `${(month.absent / total) * 100}%` }} />
          </div>
          <div className="grid grid-cols-2 gap-2">
            <Stat icon={CircleCheck} label="Присутствовал" value={month.present} className="text-tg-success" />
            <Stat icon={AlarmClock} label="Опоздал" value={month.late} className="text-tg-warning" />
            <Stat icon={CircleX} label="Пропустил" value={month.absent} className="text-tg-destructive" />
            <Stat icon={FileText} label="По уважительной" value={month.excused} className="text-tg-link" />
          </div>
        </div>
      </Section>

      <Section
        header="Аттестация по предметам"
        footer="Минимум 3 пары по предмету за месяц. Засчитываются только «был» и «опоздал»."
      >
        {month.subjects.map((subject) => {
          const passed = subject.attended >= subject.required
          const partial = !passed && subject.attended > 0
          const Icon = passed ? CircleCheck : partial ? CircleAlert : CircleX
          const tone = passed ? 'text-tg-success' : partial ? 'text-tg-warning' : 'text-tg-destructive'
          const bar = passed ? 'bg-tg-success' : partial ? 'bg-tg-warning' : 'bg-tg-destructive'
          const pct = Math.min(100, (subject.attended / subject.required) * 100)
          const left = subject.required - subject.attended
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
                    <span className={cn('block h-full rounded-full', bar)} style={{ width: `${pct}%` }} />
                  </span>
                  {!passed && <span className={cn('text-xs', tone)}>Нужно ещё {left}</span>}
                </span>
              }
            >
              {subject.name}
            </Cell>
          )
        })}
      </Section>

      <div className="px-1">
        <Button
          stretched
          size="l"
          mode="bezeled"
          before={<CalendarDays className="size-5" aria-hidden="true" />}
          onClick={() => setShowPrevious((v) => !v)}
        >
          {showPrevious ? 'Текущий месяц' : 'Прошлый месяц'}
        </Button>
      </div>

      {can.manageAttendance && (
        <Section header="Управление явкой">
          <NavCell icon={PencilLine} tile="bg-[#2481cc]" onClick={() => onOpen('mark')}>
            Отметить вручную
          </NavCell>
          <NavCell icon={Vote} tile="bg-[#8b5cf6]" onClick={() => onOpen('vote')}>
            Запустить голосование
          </NavCell>
          <NavCell icon={ChartNoAxesColumn} tile="bg-[#31b545]" onClick={() => onOpen('report')}>
            Отчёт за неделю
          </NavCell>
        </Section>
      )}

      <Section header="Группа">
        <NavCell
          icon={can.manageGroup ? Settings2 : Users}
          tile="bg-[#f59e0b]"
          onClick={() => onOpen('group')}
        >
          {can.manageGroup ? 'Управление группой' : 'Список группы'}
        </NavCell>
      </Section>
    </List>
  )
}

function NavCell({
  icon: Icon,
  tile,
  onClick,
  children,
}: {
  icon: LucideIcon
  tile: string
  onClick: () => void
  children: React.ReactNode
}) {
  return (
    <Cell
      before={
        <span className={cn('flex size-7 items-center justify-center rounded-lg text-white', tile)}>
          <Icon className="size-4" aria-hidden="true" />
        </span>
      }
      after={<ChevronRight className="size-5 text-tg-hint" aria-hidden="true" />}
      onClick={onClick}
    >
      {children}
    </Cell>
  )
}

function Stat({ icon: Icon, label, value, className }: { icon: LucideIcon; label: string; value: number; className: string }) {
  return (
    <div className="flex items-center gap-3 rounded-xl bg-tg-secondary-bg px-3 py-2.5">
      <Icon className={cn('size-5 shrink-0', className)} aria-hidden="true" />
      <div className="flex min-w-0 flex-col">
        <span className="text-lg font-bold leading-tight tabular-nums">{value}</span>
        <span className="truncate text-xs text-tg-hint">{label}</span>
      </div>
    </div>
  )
}
