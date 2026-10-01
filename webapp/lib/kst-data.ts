export type LessonStatus = 'plan' | 'replace' | 'cancel' | 'self'

export type Lesson = {
  number: number
  subject: string
  teacher: string
  room: string
  status: LessonStatus
  note?: string
}

export type DayKey = 'mon' | 'tue' | 'wed' | 'thu' | 'fri' | 'sat'

export const DAYS: { key: DayKey; short: string; full: string; date: string }[] = [
  { key: 'mon', short: 'Пн', full: 'Понедельник', date: '29 сентября' },
  { key: 'tue', short: 'Вт', full: 'Вторник', date: '30 сентября' },
  { key: 'wed', short: 'Ср', full: 'Среда', date: '1 октября' },
  { key: 'thu', short: 'Чт', full: 'Четверг', date: '2 октября' },
  { key: 'fri', short: 'Пт', full: 'Пятница', date: '3 октября' },
  { key: 'sat', short: 'Сб', full: 'Суббота', date: '4 октября' },
]

export const TODAY_KEY: DayKey = 'wed'
export const TODAY_ISO = '2025-10-01'
export const IS_ODD_WEEK = true

export const LESSON_TIMES: Record<number, string> = {
  1: '09:00–10:35',
  2: '10:45–12:20',
  3: '13:00–14:35',
  4: '14:45–16:20',
  5: '16:30–18:05',
}

export const STUDENT = {
  name: 'Иван Петров',
  username: '@ivan_petrov',
  group: '25КАД',
}

export const SCHEDULE: Record<DayKey, Lesson[]> = {
  mon: [
    { number: 1, subject: 'Математика', teacher: 'Соколова Е. В.', room: '204', status: 'plan' },
    { number: 2, subject: 'Инженерная графика', teacher: 'Ковалёв Д. А.', room: '312', status: 'plan' },
    { number: 3, subject: 'Физическая культура', teacher: 'Медведев С. П.', room: 'Спортзал', status: 'plan' },
  ],
  tue: [
    { number: 1, subject: 'История', teacher: 'Орлова Н. И.', room: '108', status: 'plan' },
    { number: 2, subject: 'Строительные материалы', teacher: 'Громов А. В.', room: '221', status: 'plan' },
    { number: 3, subject: 'Английский язык', teacher: 'Белова М. С.', room: '115', status: 'self' },
    { number: 4, subject: 'Информатика', teacher: 'Захаров И. О.', room: '401', status: 'plan' },
  ],
  wed: [
    { number: 1, subject: 'Математика', teacher: 'Соколова Е. В.', room: '204', status: 'plan' },
    {
      number: 2,
      subject: 'Архитектура зданий',
      teacher: 'Лебедева О. Н.',
      room: '305',
      status: 'replace',
      note: 'Вместо «Литература»',
    },
    { number: 3, subject: 'Литература', teacher: 'Миронова Т. А.', room: '110', status: 'cancel' },
    { number: 4, subject: 'Инженерная графика', teacher: 'Ковалёв Д. А.', room: '312', status: 'self' },
  ],
  thu: [
    { number: 1, subject: 'Физика', teacher: 'Кузнецов В. Г.', room: '208', status: 'plan' },
    { number: 2, subject: 'Математика', teacher: 'Соколова Е. В.', room: '204', status: 'plan' },
    { number: 3, subject: 'Строительные материалы', teacher: 'Громов А. В.', room: '221', status: 'plan' },
  ],
  fri: [
    { number: 2, subject: 'Английский язык', teacher: 'Белова М. С.', room: '115', status: 'plan' },
    { number: 3, subject: 'Информатика', teacher: 'Захаров И. О.', room: '401', status: 'replace', note: 'Перенос из 402' },
    { number: 4, subject: 'История', teacher: 'Орлова Н. И.', room: '108', status: 'plan' },
  ],
  sat: [{ number: 1, subject: 'Архитектура зданий', teacher: 'Лебедева О. Н.', room: '305', status: 'plan' }],
}

export type DueMode = 'lessons' | 'date'

export type Deadline = {
  id: string
  title: string
  subject: string
  teacher: string
  dueMode: DueMode
  dueLessons?: number
  dueLessonNumber?: number
  dueISO: string
}

const WEEKDAY_TO_KEY: Record<number, DayKey | undefined> = {
  1: 'mon',
  2: 'tue',
  3: 'wed',
  4: 'thu',
  5: 'fri',
  6: 'sat',
}

export function addDaysISO(iso: string, days: number) {
  const d = new Date(`${iso}T00:00:00Z`)
  d.setUTCDate(d.getUTCDate() + days)
  return d.toISOString().slice(0, 10)
}

export function teacherFor(subject: string) {
  for (const day of Object.values(SCHEDULE)) {
    const lesson = day.find((l) => l.subject === subject)
    if (lesson) return lesson.teacher
  }
  return '—'
}

export function findLessonOccurrence(subject: string, n: number) {
  let count = 0
  for (let offset = 1; offset <= 120; offset++) {
    const iso = addDaysISO(TODAY_ISO, offset)
    const key = WEEKDAY_TO_KEY[new Date(`${iso}T00:00:00Z`).getUTCDay()]
    if (!key) continue
    for (const lesson of SCHEDULE[key]) {
      if (lesson.subject !== subject || lesson.status === 'cancel') continue
      count++
      if (count === n) return { iso, lessonNumber: lesson.number }
    }
  }
  return null
}

function lessonDeadline(id: string, title: string, subject: string, n: number): Deadline {
  const occ = findLessonOccurrence(subject, n)
  return {
    id,
    title,
    subject,
    teacher: teacherFor(subject),
    dueMode: 'lessons',
    dueLessons: n,
    dueLessonNumber: occ?.lessonNumber,
    dueISO: occ?.iso ?? TODAY_ISO,
  }
}

function dateDeadline(id: string, title: string, subject: string, dueISO: string): Deadline {
  return { id, title, subject, teacher: teacherFor(subject), dueMode: 'date', dueISO }
}

export const INITIAL_DEADLINES: Deadline[] = [
  dateDeadline('d1', 'Реферат по эпохе Петра I', 'История', '2025-09-29'),
  dateDeadline('d2', 'Чертёж фасада, лист А3', 'Инженерная графика', '2025-10-01'),
  lessonDeadline('d3', 'Контрольная: производные', 'Математика', 1),
  lessonDeadline('d4', 'Лабораторная №3', 'Строительные материалы', 1),
  lessonDeadline('d5', 'Эссе на английском', 'Английский язык', 2),
  dateDeadline('d6', 'Проект: план этажа', 'Архитектура зданий', '2025-10-14'),
]

export function plural(n: number, one: string, few: string, many: string) {
  const m10 = n % 10
  const m100 = n % 100
  if (m10 === 1 && m100 !== 11) return one
  if (m10 >= 2 && m10 <= 4 && (m100 < 12 || m100 > 14)) return few
  return many
}

export function lessonsLabel(n: number) {
  return n === 1 ? 'На следующую пару' : `Через ${n} ${plural(n, 'занятие', 'занятия', 'занятий')}`
}

export function daysFromToday(iso: string) {
  const day = 24 * 60 * 60 * 1000
  return Math.round((Date.parse(`${iso}T00:00:00Z`) - Date.parse(`${TODAY_ISO}T00:00:00Z`)) / day)
}

export function relativeLabel(iso: string) {
  const diff = daysFromToday(iso)
  if (diff === 0) return 'Сегодня'
  if (diff === 1) return 'Завтра'
  if (diff === 2) return 'Послезавтра'
  if (diff < 0) return `Просрочено на ${-diff} ${plural(-diff, 'день', 'дня', 'дней')}`
  return `Через ${diff} ${plural(diff, 'день', 'дня', 'дней')}`
}

export function dateParts(iso: string) {
  const d = new Date(`${iso}T00:00:00Z`)
  return {
    day: d.getUTCDate(),
    month: d.toLocaleDateString('ru-RU', { month: 'short', timeZone: 'UTC' }).replace('.', ''),
    weekday: d.toLocaleDateString('ru-RU', { weekday: 'short', timeZone: 'UTC' }),
    long: d.toLocaleDateString('ru-RU', { weekday: 'long', day: 'numeric', month: 'long', timeZone: 'UTC' }),
  }
}

export type Role = 'student' | 'deputy' | 'starosta' | 'admin'

export const ROLE_LABEL: Record<Role, string> = {
  student: 'Студент',
  deputy: 'Зам. старосты',
  starosta: 'Староста',
  admin: 'Админ',
}

export function permissions(role: Role) {
  return {
    manageAttendance: role !== 'student',
    manageGroup: role === 'starosta' || role === 'admin',
    isAdmin: role === 'admin',
  }
}

export type MarkStatus = 'present' | 'late' | 'absent' | 'excused'

export const MARK_LABEL: Record<MarkStatus, string> = {
  present: 'Был',
  late: 'Опоздал',
  absent: 'Не был',
  excused: 'Уваж.',
}

export type AttendanceMode = 'chat' | 'dm'

export type GroupMember = { id: string; name: string; role: Role }

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

export const SELF_ID = 'u1'

export const ACTIVE_LESSON = {
  code: 'ОД.07',
  subject: 'Математика',
  number: 1,
  time: LESSON_TIMES[1],
  date: '01.10',
}

export const POLL_WINDOW_SECONDS = 5 * 60

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

export function generateInviteCode() {
  return String(Math.floor(100000 + Math.random() * 900000))
}

export type AttendanceMonth = {
  label: string
  present: number
  late: number
  absent: number
  excused: number
  subjects: { name: string; attended: number; required: number }[]
}

export const ATTENDANCE: { current: AttendanceMonth; previous: AttendanceMonth } = {
  current: {
    label: 'Октябрь',
    present: 18,
    late: 3,
    absent: 2,
    excused: 1,
    subjects: [
      { name: 'Математика', attended: 5, required: 3 },
      { name: 'Инженерная графика', attended: 3, required: 3 },
      { name: 'История', attended: 2, required: 3 },
      { name: 'Строительные материалы', attended: 1, required: 3 },
      { name: 'Литература', attended: 0, required: 3 },
    ],
  },
  previous: {
    label: 'Сентябрь',
    present: 54,
    late: 6,
    absent: 4,
    excused: 2,
    subjects: [
      { name: 'Математика', attended: 12, required: 8 },
      { name: 'Инженерная графика', attended: 8, required: 8 },
      { name: 'История', attended: 7, required: 8 },
      { name: 'Строительные материалы', attended: 9, required: 8 },
      { name: 'Литература', attended: 3, required: 8 },
    ],
  },
}

export const SUBJECTS = Array.from(
  new Set(Object.values(SCHEDULE).flatMap((day) => day.map((l) => l.subject))),
).sort((a, b) => a.localeCompare(b, 'ru'))

export type Urgency = 'overdue' | 'today' | 'tomorrow' | 'later'

export function getUrgency(iso: string): Urgency {
  const diff = daysFromToday(iso)
  if (diff < 0) return 'overdue'
  if (diff === 0) return 'today'
  if (diff === 1) return 'tomorrow'
  return 'later'
}
