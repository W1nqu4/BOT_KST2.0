'use client'

import { useMemo, useState } from 'react'
import { AppRoot, Tabbar } from '@telegram-apps/telegram-ui'
import { CalendarDays, ChartColumn, House, ListTodo, UserRound, type LucideIcon } from 'lucide-react'

import type { Profile } from '@/lib/api-types'
import { GROUP_MEMBERS, generateInviteCode, type AttendanceMode, type GroupMember } from '@/lib/kst-data'
import { permissions } from '@/lib/roles'
import { haptic, notify, useTelegramBackButton, useTelegramInit } from '@/lib/telegram'
import { useApi } from '@/lib/use-api'
import { useTheme } from '@/lib/use-theme'
import { ScreenHeader } from './screen-header'
import { HomeScreen } from './screens/home-screen'
import { WeekScreen } from './screens/week-screen'
import { DeadlinesScreen } from './screens/deadlines-screen'
import { AddDeadlineScreen } from './screens/add-deadline-screen'
import { AttendanceScreen } from './screens/attendance-screen'
import { ProfileScreen } from './screens/profile-screen'
import { EditNameScreen } from './screens/edit-name-screen'
import { GroupScreen } from './screens/group-screen'
import { MarkScreen } from './screens/mark-screen'
import { ReportScreen } from './screens/report-screen'
import { VoteScreen } from './screens/vote-screen'

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

/**
 * Корневой компонент Mini App: таббар, стек экранов и тема.
 *
 * Роль берётся из ``/api/profile``: от неё зависит, показывать ли функции
 * старосты (отметки, голосование, отчёт). Стек экранов — обычный ``useState``,
 * без роутера: маршрутизация не нужна, а back-кнопку Telegram подключаем сами.
 */
export function KstApp() {
  useTelegramInit()
  useTheme()

  const [stack, setStack] = useState<ScreenKey[]>(['home'])
  const profile = useApi<Profile>('/api/profile')
  // Локальное состояние моков функций старосты (до появления /api/group).
  const [members, setMembers] = useState<GroupMember[]>(GROUP_MEMBERS)
  const [mode, setMode] = useState<AttendanceMode>('chat')
  const [inviteCode, setInviteCode] = useState(generateInviteCode)
  const [localName, setLocalName] = useState<string | null>(null)

  const role = profile.data?.role ?? 'student'
  const can = useMemo(() => permissions(role), [role])

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

  // Ссылку на бота из Mini App дать нельзя — объясняем, где выбрать группу.
  const explainGroup = () =>
    notify('Откройте чат с ботом и отправьте /start — он попросит номер группы. После этого расписание появится здесь.')

  return (
    <AppRoot className="min-h-dvh bg-tg-secondary-bg text-tg-text">
      <div className="mx-auto flex min-h-dvh max-w-md flex-col">
        <ScreenHeader title={TITLES[current]} onBack={canGoBack ? goBack : undefined} />

        <main key={current} className="flex-1 animate-in pb-28 duration-300 fade-in slide-in-from-right-3">
          {current === 'home' && <HomeScreen onNeedGroup={explainGroup} />}
          {current === 'week' && <WeekScreen />}
          {current === 'deadlines' && <DeadlinesScreen onAdd={() => push('add-deadline')} />}
          {current === 'add-deadline' && (
            <AddDeadlineScreen
              onCreated={(created) => {
                notify(`Дедлайн «${created.task}» сохранён`)
                setStack(['home', 'deadlines'])
              }}
            />
          )}
          {current === 'attendance' && <AttendanceScreen onOpen={push} />}
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
          {current === 'profile' && <ProfileScreen onEditName={() => push('edit-name')} />}
          {current === 'edit-name' && (
            <EditNameScreen
              initialName={localName ?? profile.data?.name ?? ''}
              onSubmit={(value) => {
                setLocalName(value)
                notify('Имя сохранено в приложении. Чтобы изменить его для бота, напишите в чат.')
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
            className="!px-0.5 [&_span]:![text-overflow:clip] [&_span]:!whitespace-nowrap [&_span]:overflow-visible [&_span]:text-[11px]"
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