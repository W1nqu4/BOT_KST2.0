/**
 * Моки экранов старосты: отметки, голосование, отчёт и группа.
 *
 * Расписание, дедлайны, профиль и явка уже работают на реальном API
 * (`lib/api.ts`). Эти четыре экрана пока демонстрационные: их действия в боте
 * выполняются кнопками и голосованием в чате, а API для группы появится
 * отдельной задачей. Когда он появится, файл нужно удалить целиком.
 *
 * Роли, подписи и права живут в `lib/roles.ts` — там настоящие данные с
 * бэкенда (`/api/profile`), здесь их дублировать нельзя.
 */

/** Режим опроса: в чат группы или каждому в личку (совпадает с backend). */
export type AttendanceMode = 'chat' | 'dm'

/** Статусы отметки в ручном режиме. */
export type MarkStatus = 'present' | 'late' | 'absent' | 'excused'

export const MARK_LABEL: Record<MarkStatus, string> = {
  present: 'Был',
  late: 'Опоздал',
  absent: 'Не был',
  excused: 'Уваж.',
}

/** Участник группы для демо-списков. */
export type GroupMember = { id: string; name: string; role: import('./api-types').Role }

export const GROUP_MEMBERS: GroupMember[] = [
  { id: 'u1', name: 'Петров И. А.', role: 'starosta' },
  { id: 'u2', name: 'Абрамчик С. Г.', role: 'deputy' },
  { id: 'u3', name: 'Васильева А. Д.', role: 'student' },
  { id: 'u4', name: 'Григорьев М. Е.', role: 'student' },
  { id: 'u5', name: 'Иванов И. И.', role: 'student' },
  { id: 'u6', name: 'Котова Е. Р.', role: 'student' },
  { id: 'u7', name: 'Петров П. П.', role: 'student' },
  { id: 'u8', name: 'Сидоров С. С.', role: 'student' },
]

/** Кто «я» в демо-списке: нужен, чтобы не давать отмечать себя. */
export const SELF_ID = 'u1'

/** Пара для демо-опроса и голосования. */
export const ACTIVE_LESSON = {
  code: 'ОД.07',
  subject: 'Математика',
  number: 1,
  time: '09:00–10:35',
  date: '01.10',
}

/** Группа для шапок демо-экранов (реальная приходит из /api/profile). */
export const STUDENT = {
  name: 'Иван Петров',
  username: '@ivan_petrov',
  group: '25КАД',
}

/** Замены для демо-списка ответов. */
export const SUBJECTS = ['Математика', 'История', 'Литература']

/** Отчёт за неделю (демо). */
export const WEEK_REPORT = {
  range: '22–28 сентября',
  atRisk: [
    { name: 'Иванов И. И.', items: ['История 2/3', 'Литература 1/3'] },
    { name: 'Петров П. П.', items: ['Литература 0/3'] },
  ],
  truants: [
    { name: 'Петров П. П.', count: 3 },
    { name: 'Григорьев М. Е.', count: 2 },
  ],
  excellent: ['Абрамчик С. Г.', 'Котова Е. Р.'],
}

/** 6-значный код приглашения (демо): ведущий ноль исключён, как в боте. */
export function generateInviteCode() {
  return String(Math.floor(100000 + Math.random() * 900000))
}

/** Русское склонение: plural(3, 'пара', 'пары', 'пар') → «пары». */
export function plural(count: number, one: string, few: string, many: string): string {
  const hundred = Math.abs(count) % 100
  if (hundred >= 11 && hundred <= 14) return many
  const ten = Math.abs(count) % 10
  if (ten === 1) return one
  if (ten >= 2 && ten <= 4) return few
  return many
}