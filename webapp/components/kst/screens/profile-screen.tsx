'use client'

import { Avatar, Cell, List, Section } from '@telegram-apps/telegram-ui'
import { Bell, Bug, CalendarSync, ChevronRight, GraduationCap, Pencil, ShieldCheck, type LucideIcon } from 'lucide-react'

import type { Profile, Role } from '@/lib/api-types'
import { ROLE_LABEL } from '@/lib/roles'
import { useApi } from '@/lib/use-api'
import { notify } from '@/lib/telegram'
import { cn } from '@/lib/utils'
import { ErrorState, SkeletonList } from '../async-state'

type Props = { onEditName: () => void }

/**
 * Профиль студента: имя, группа и роль из ``/api/profile``.
 *
 * Роль больше не переключается вручную (в макете была демо-кнопка «просмотр
 * как»): её задаёт бэкенд — ``students.role`` или ``ADMIN_IDS``. Показывать
 * студенту кнопки управления группой нельзя.
 */
export function ProfileScreen({ onEditName }: Props) {
  const { data, loading, error, refresh } = useApi<Profile>('/api/profile')

  if (loading && !data) return <SkeletonList rows={3} />
  if (error && !data) return <ErrorState message={error} onRetry={refresh} />

  const name = data?.name || 'Студент'
  const initials = name
    .split(' ')
    .map((part) => part[0])
    .join('')
    .slice(0, 2)
    .toUpperCase()
  const role: Role = data?.role ?? 'student'

  return (
    <List>
      <section className="flex flex-col items-center gap-2 pt-4 pb-2 text-center">
        <Avatar size={96} acronym={initials || 'КСТ'} />
        <h2 className="mt-1 text-2xl font-bold text-balance">{name}</h2>
        <div className="flex gap-2">
          {data?.group && (
            <span className="rounded-full bg-tg-section px-3 py-1 text-sm font-semibold">{data.group}</span>
          )}
          <span className="rounded-full bg-tg-fill px-3 py-1 text-sm font-semibold text-tg-link">{ROLE_LABEL[role]}</span>
        </div>
      </section>

      <Section header="Информация">
        <Cell
          before={<IconTile icon={GraduationCap} className="bg-tg-button" />}
          after={<span className="text-tg-hint">{data?.group || '—'}</span>}
        >
          Группа
        </Cell>
        <Cell
          before={<IconTile icon={ShieldCheck} className="bg-tg-success" />}
          after={<span className="text-tg-hint">{ROLE_LABEL[role]}</span>}
        >
          Роль
        </Cell>
      </Section>

      <Section header="Настройки">
        <Cell
          before={<IconTile icon={Pencil} className="bg-tg-warning" />}
          after={<ChevronRight className="size-5 text-tg-hint" aria-hidden="true" />}
          onClick={onEditName}
        >
          Изменить имя
        </Cell>
        <Cell
          before={<IconTile icon={CalendarSync} className="bg-tg-button" />}
          after={<ChevronRight className="size-5 text-tg-hint" aria-hidden="true" />}
          onClick={() =>
            notify('Ссылка для подписки на календарь приходит в чате с ботом: «Настройки» → «Календарь».')
          }
        >
          Интеграция с календарём
        </Cell>
        <Cell
          before={<IconTile icon={Bell} className="bg-tg-accent" />}
          after={<ChevronRight className="size-5 text-tg-hint" aria-hidden="true" />}
          onClick={() => notify('Напоминания включаются в чате с ботом командой /settings.')}
        >
          Уведомления
        </Cell>
        <Cell
          before={<IconTile icon={Bug} className="bg-tg-danger" />}
          after={<ChevronRight className="size-5 text-tg-hint" aria-hidden="true" />}
          onClick={() => notify('Опишите проблему в чате с ботом — мы ответим как можно скорее.')}
        >
          Сообщить о проблеме
        </Cell>
      </Section>

      <p className="pb-4 text-center text-xs text-tg-hint">КСТ · Красноярский строительный техникум</p>
    </List>
  )
}

function IconTile({ icon: Icon, className }: { icon: LucideIcon; className: string }) {
  return (
    <span className={cn('flex size-7 items-center justify-center rounded-lg text-white', className)}>
      <Icon className="size-4" aria-hidden="true" />
    </span>
  )
}
