'use client'

import { Cell, List, Section } from '@telegram-apps/telegram-ui'
import {
  Check,
  Copy,
  Database,
  Megaphone,
  MessagesSquare,
  RefreshCw,
  Send,
  ShieldCheck,
  Trash2,
  UserMinus,
  UsersRound,
  type LucideIcon,
} from 'lucide-react'
import {
  ROLE_LABEL,
  SELF_ID,
  STUDENT,
  generateInviteCode,
  permissions,
  type AttendanceMode,
  type GroupMember,
  type Role,
} from '@/lib/kst-data'
import { haptic, notify } from '@/lib/telegram'
import { cn } from '@/lib/utils'

type Props = {
  role: Role
  members: GroupMember[]
  onMembersChange: (members: GroupMember[]) => void
  mode: AttendanceMode
  onModeChange: (mode: AttendanceMode) => void
  inviteCode: string
  onInviteCodeChange: (code: string) => void
}

const MODES: { key: AttendanceMode; title: string; description: string; icon: LucideIcon }[] = [
  { key: 'chat', title: 'Чат группы', description: 'Опрос приходит в общий чат', icon: MessagesSquare },
  { key: 'dm', title: 'Личка', description: 'Каждому студенту приватно', icon: Send },
]

export function GroupScreen({ role, members, onMembersChange, mode, onModeChange, inviteCode, onInviteCodeChange }: Props) {
  const can = permissions(role)

  const toggleDeputy = (member: GroupMember) => {
    haptic()
    const nextRole: Role = member.role === 'deputy' ? 'student' : 'deputy'
    onMembersChange(
      members.map((m) => {
        if (m.id === member.id) return { ...m, role: nextRole }
        if (nextRole === 'deputy' && m.role === 'deputy') return { ...m, role: 'student' }
        return m
      }),
    )
  }

  const remove = (member: GroupMember) => {
    if (!window.confirm(`Удалить ${member.name} из группы?`)) return
    onMembersChange(members.filter((m) => m.id !== member.id))
  }

  return (
    <List>
      <section className="flex flex-col items-center gap-1 pt-4 pb-2 text-center">
        <span className="flex size-16 items-center justify-center rounded-2xl bg-tg-button text-tg-button-text">
          <UsersRound className="size-8" aria-hidden="true" />
        </span>
        <h2 className="mt-2 text-2xl font-bold">{STUDENT.group}</h2>
        <p className="text-sm text-tg-hint">
          {members.length} участников · {ROLE_LABEL[role]}
        </p>
      </section>

      {can.manageGroup && (
        <Section header="Код приглашения" footer="Студенты вводят этот код, чтобы вступить в группу.">
          <div className="flex items-center justify-between gap-3 p-4">
            <span className="font-mono text-3xl font-bold tracking-[0.3em] tabular-nums">{inviteCode}</span>
            <div className="flex gap-2">
              <IconButton
                label="Скопировать код"
                icon={Copy}
                onClick={() => {
                  navigator.clipboard?.writeText(inviteCode)
                  notify('Код скопирован')
                }}
              />
              <IconButton
                label="Создать новый код"
                icon={RefreshCw}
                onClick={() => {
                  haptic()
                  onInviteCodeChange(generateInviteCode())
                }}
              />
            </div>
          </div>
        </Section>
      )}

      {can.manageGroup && (
        <Section header="Режим посещаемости" footer="Смена применяется к новым опросам — активные доживают в старом режиме.">
          {MODES.map(({ key, title, description, icon: Icon }) => (
            <Cell
              key={key}
              role="radio"
              aria-checked={mode === key}
              before={<Icon className="size-6 text-tg-link" aria-hidden="true" />}
              description={description}
              after={mode === key ? <Check className="size-5 text-tg-link" aria-hidden="true" /> : null}
              onClick={() => {
                haptic()
                onModeChange(key)
              }}
            >
              {title}
            </Cell>
          ))}
        </Section>
      )}

      <Section
        header="Участники"
        footer={can.manageGroup ? 'Нажми на щит, чтобы назначить или снять зама. Зам может быть только один.' : undefined}
      >
        {members.map((m) => {
          const isSelf = m.id === SELF_ID
          const editable = can.manageGroup && !isSelf && m.role !== 'starosta'
          return (
            <Cell
              key={m.id}
              Component="div"
              subtitle={ROLE_LABEL[m.role]}
              after={
                editable ? (
                  <div className="flex gap-1">
                    <IconButton
                      label={m.role === 'deputy' ? `Снять зама: ${m.name}` : `Назначить замом: ${m.name}`}
                      icon={ShieldCheck}
                      active={m.role === 'deputy'}
                      onClick={() => toggleDeputy(m)}
                    />
                    <IconButton label={`Удалить: ${m.name}`} icon={UserMinus} tone="destructive" onClick={() => remove(m)} />
                  </div>
                ) : null
              }
            >
              {m.name}
              {isSelf && <span className="text-tg-hint"> · вы</span>}
            </Cell>
          )
        })}
      </Section>

      {can.isAdmin && (
        <Section header="Администрирование">
          <AdminCell icon={UsersRound} tile="bg-[#2481cc]" text="Все пользователи бота" />
          <AdminCell icon={Megaphone} tile="bg-[#f59e0b]" text="Рассылка всем студентам" />
          <AdminCell icon={RefreshCw} tile="bg-[#8b5cf6]" text="Принудительный парсинг" />
          <AdminCell icon={Database} tile="bg-[#31b545]" text="Скачать бэкап БД" />
          <AdminCell icon={Trash2} tile="bg-[#e53935]" text="Удалить группу" />
        </Section>
      )}
    </List>
  )
}

function AdminCell({ icon: Icon, tile, text }: { icon: LucideIcon; tile: string; text: string }) {
  return (
    <Cell
      before={
        <span className={cn('flex size-7 items-center justify-center rounded-lg text-white', tile)}>
          <Icon className="size-4" aria-hidden="true" />
        </span>
      }
      onClick={() => notify(`${text}: доступно в чате с ботом.`)}
    >
      {text}
    </Cell>
  )
}

function IconButton({
  label,
  icon: Icon,
  onClick,
  active,
  tone,
}: {
  label: string
  icon: LucideIcon
  onClick: () => void
  active?: boolean
  tone?: 'destructive'
}) {
  return (
    <button
      type="button"
      aria-label={label}
      aria-pressed={active}
      onClick={onClick}
      className={cn(
        'flex size-9 items-center justify-center rounded-full transition-colors active:opacity-60',
        active
          ? 'bg-tg-link text-white'
          : tone === 'destructive'
            ? 'bg-tg-destructive/15 text-tg-destructive'
            : 'bg-tg-secondary-bg text-tg-link',
      )}
    >
      <Icon className="size-[18px]" aria-hidden="true" />
    </button>
  )
}
