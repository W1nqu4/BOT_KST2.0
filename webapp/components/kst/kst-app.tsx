'use client'

import { useState } from 'react'
import { AppRoot, Tabbar } from '@telegram-apps/telegram-ui'
import { CalendarDays, ChartColumn, House, ListTodo, UserRound, type LucideIcon } from 'lucide-react'
import {
  GROUP_MEMBERS,
  INITIAL_DEADLINES,
  STUDENT,
  generateInviteCode,
  type AttendanceMode,
  type Deadline,
  type Role,
} from '@/lib/kst-data'
import { MarkScreen } from './screens/mark-screen'
import { VoteScreen } from './screens/vote-screen'
import { ReportScreen } from './screens/report-screen'
import { GroupScreen } from './screens/group-screen'
import { haptic, useTelegramBackButton, useTelegramInit } from '@/lib/telegram'
import { ScreenHeader } from './screen-header'
import { HomeScreen } from './screens/home-screen'
import { WeekScreen } from './screens/week-screen'
import { DeadlinesScreen } from './screens/deadlines-screen'
import { AddDeadlineScreen } from './screens/add-deadline-screen'
import { AttendanceScreen } from './screens/attendance-screen'
import { ProfileScreen } from './screens/profile-screen'
import { EditNameScreen } from './screens/edit-name-screen'

type TabKey = 'home' | 'week' | 'deadlines' | 'attendance' | 'profile'
type ScreenKey = TabKey | 'add-deadline' | 'edit-name' | 'mark' | 'vote' | 'report' | 'group'

const TABS: { key: TabKey; label: string; icon: LucideIcon }[] = [
  { key: 'home', label: 'Главная', icon: House },
  { key: 'week', label: 'Неделя', icon: CalendarDays },
  { key: 'deadlines', label: 'Дедлайны', icon: ListTodo },
  { key: 'attendance', label: 'Явка', icon: ChartColumn },
  { key: 'profile', label: 'Профиль', icon: UserRound },
]

const TITLES: Record<ScreenKey, string> = {
  home: 'Сегодня',
  week: 'Расписание на неделю',
  deadlines: 'Дедлайны',
  attendance: 'Явка',
  profile: 'Профиль',
  'add-deadline': 'Новый дедлайн',
  'edit-name': 'Изменить имя',
  mark: 'Отметить вручную',
  vote: 'Голосование',
  report: 'Отчёт за неделю',
  group: 'Моя группа',
}

const PARENT_TAB: Record<ScreenKey, TabKey> = {
  home: 'home',
  week: 'week',
  deadlines: 'deadlines',
  attendance: 'attendance',
  profile: 'profile',
  'add-deadline': 'deadlines',
  'edit-name': 'profile',
  mark: 'attendance',
  vote: 'attendance',
  report: 'attendance',
  group: 'attendance',
}

export function KstApp() {
  useTelegramInit()

  const [stack, setStack] = useState<ScreenKey[]>(['home'])
  const [deadlines, setDeadlines] = useState<Deadline[]>(INITIAL_DEADLINES)
  const [name, setName] = useState(STUDENT.name)
  const [role, setRole] = useState<Role>('starosta')
  const [mode, setMode] = useState<AttendanceMode>('chat')
  const [members, setMembers] = useState(GROUP_MEMBERS)
  const [inviteCode, setInviteCode] = useState(generateInviteCode)

  const current = stack[stack.length - 1]
  const activeTab = PARENT_TAB[current]
  const canGoBack = stack.length > 1

  const goBack = () => setStack((s) => (s.length > 1 ? s.slice(0, -1) : s))
  const push = (screen: ScreenKey) => setStack((s) => [...s, screen])
  const selectTab = (tab: TabKey) => {
    haptic()
    setStack(tab === 'home' ? ['home'] : ['home', tab])
  }

  useTelegramBackButton(canGoBack, goBack)

  return (
    <AppRoot className="min-h-dvh bg-tg-secondary-bg text-tg-text">
      <div className="mx-auto flex min-h-dvh max-w-md flex-col">
        <ScreenHeader title={TITLES[current]} onBack={canGoBack ? goBack : undefined} />

        <main key={current} className="flex-1 pb-28 animate-in fade-in slide-in-from-right-3 duration-300">
          {current === 'home' && <HomeScreen />}
          {current === 'week' && <WeekScreen />}
          {current === 'deadlines' && (
            <DeadlinesScreen
              deadlines={deadlines}
              onAdd={() => push('add-deadline')}
              onToggleDone={(id) => setDeadlines((list) => list.filter((d) => d.id !== id))}
            />
          )}
          {current === 'add-deadline' && (
            <AddDeadlineScreen
              onSubmit={(deadline) => {
                setDeadlines((list) => [...list, deadline])
                goBack()
              }}
            />
          )}
          {current === 'attendance' && <AttendanceScreen role={role} mode={mode} onOpen={push} />}
          {current === 'mark' && <MarkScreen members={members} />}
          {current === 'vote' && <VoteScreen members={members} />}
          {current === 'report' && <ReportScreen />}
          {current === 'group' && (
            <GroupScreen
              role={role}
              members={members}
              onMembersChange={setMembers}
              mode={mode}
              onModeChange={setMode}
              inviteCode={inviteCode}
              onInviteCodeChange={setInviteCode}
            />
          )}
          {current === 'profile' && (
            <ProfileScreen name={name} role={role} onRoleChange={setRole} onEditName={() => push('edit-name')} />
          )}
          {current === 'edit-name' && (
            <EditNameScreen
              initialName={name}
              onSubmit={(value) => {
                setName(value)
                goBack()
              }}
            />
          )}
        </main>
      </div>

      <Tabbar>
        {TABS.map(({ key, label, icon: Icon }) => (
          <Tabbar.Item
              key={key}
              className="!px-0.5 [&_span]:!overflow-visible [&_span]:!text-[11px] [&_span]:!whitespace-nowrap [&_span]:![text-overflow:clip]"
              text={label}
            selected={activeTab === key}
            onClick={() => selectTab(key)}
            aria-current={activeTab === key ? 'page' : undefined}
          >
            <Icon className="size-6" aria-hidden="true" />
          </Tabbar.Item>
        ))}
      </Tabbar>
    </AppRoot>
  )
}
