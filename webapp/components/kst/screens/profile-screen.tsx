'use client'

import { Avatar, Cell, List, Section } from '@telegram-apps/telegram-ui'
import { Bug, CalendarSync, ChevronRight, GraduationCap, Pencil, ShieldCheck, type LucideIcon } from 'lucide-react'
import { ROLE_LABEL, STUDENT, type Role } from '@/lib/kst-data'
import { haptic, notify } from '@/lib/telegram'
import { cn } from '@/lib/utils'

const ROLES: Role[] = ['student', 'deputy', 'starosta', 'admin']

type Props = { name: string; role: Role; onRoleChange: (role: Role) => void; onEditName: () => void }

export function ProfileScreen({ name, role, onRoleChange, onEditName }: Props) {
  const initials = name
    .split(' ')
    .map((p) => p[0])
    .join('')
    .slice(0, 2)
    .toUpperCase()

  return (
    <List>
      <section className="flex flex-col items-center gap-2 pt-4 pb-2 text-center">
        <Avatar size={96} acronym={initials} />
        <h2 className="mt-1 text-2xl font-bold text-balance">{name}</h2>
        <p className="text-sm text-tg-hint">{STUDENT.username}</p>
        <div className="flex gap-2">
          <span className="rounded-full bg-tg-section px-3 py-1 text-sm font-semibold">{STUDENT.group}</span>
          <span className="rounded-full bg-tg-fill px-3 py-1 text-sm font-semibold text-tg-link">{ROLE_LABEL[role]}</span>
        </div>
      </section>

      <Section header="Информация">
        <Cell before={<IconTile icon={GraduationCap} className="bg-[#2481cc]" />} after={<span className="text-tg-hint">{STUDENT.group}</span>}>
          Группа
        </Cell>
        <Cell before={<IconTile icon={ShieldCheck} className="bg-[#31b545]" />} after={<span className="text-tg-hint">{ROLE_LABEL[role]}</span>}>
          Роль
        </Cell>
      </Section>

      <Section header="Просмотр как" footer="Демо: переключи роль, чтобы увидеть, что доступно каждому во вкладке «Явка».">
        <div className="grid grid-cols-2 gap-2 p-3" role="radiogroup" aria-label="Роль">
          {ROLES.map((r) => (
            <button
              key={r}
              type="button"
              role="radio"
              aria-checked={role === r}
              onClick={() => {
                haptic()
                onRoleChange(r)
              }}
              className={cn(
                'h-10 rounded-xl text-sm font-semibold transition-colors',
                role === r ? 'bg-tg-button text-tg-button-text' : 'bg-tg-secondary-bg text-tg-text',
              )}
            >
              {ROLE_LABEL[r]}
            </button>
          ))}
        </div>
      </Section>

      <Section header="Настройки">
        <Cell
          before={<IconTile icon={Pencil} className="bg-[#f59e0b]" />}
          after={<ChevronRight className="size-5 text-tg-hint" aria-hidden="true" />}
          onClick={onEditName}
        >
          Изменить имя
        </Cell>
        <Cell
          before={<IconTile icon={CalendarSync} className="bg-[#2481cc]" />}
          after={<ChevronRight className="size-5 text-tg-hint" aria-hidden="true" />}
          onClick={() => notify('Ссылка для подписки на календарь скопирована. Добавьте её в Google или Apple Календарь.')}
        >
          Интеграция с календарём
        </Cell>
        <Cell
          before={<IconTile icon={Bug} className="bg-[#e53935]" />}
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
    <span className={`flex size-7 items-center justify-center rounded-lg text-white ${className}`}>
      <Icon className="size-4" aria-hidden="true" />
    </span>
  )
}
