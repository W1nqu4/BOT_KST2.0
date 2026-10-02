/**
 * Типы ответов API Mini App.
 *
 * Источник истины — бэкенд (bot/api): сериализаторы `bot/api/serializers.py`,
 * `bot/api/schedule.py`, `bot/api/deadlines.py`, `bot/api/attendance.py`.
 * Имена полей здесь обязаны совпадать с JSON один в один.
 */

/** Состояние пары. Порядок отражает приоритет иконок в боте. */
export type LessonStatus =
  | 'planned'
  | 'substituted'
  | 'cancelled'
  | 'self_study'

/** Одна пара в расписании (GET /api/schedule/*). */
export interface Lesson {
  para: number
  subject: string
  teacher: string
  room: string
  time: string
  status: LessonStatus
  /** Предмет по плану — только если была замена или отмена. */
  old_subject: string | null
  is_cancelled: boolean
  is_self_study: boolean
}

/** День расписания. `is_today` приходит только в /week и /today. */
export interface ScheduleDay {
  group: string
  date: string
  weekday: string
  /** Чётность числа месяца: «Чет» | «нечет». */
  week_type: string
  lessons: Lesson[]
  is_today?: boolean
}

/** GET /api/schedule/week */
export interface WeekPayload {
  group: string
  week_start: string
  days: ScheduleDay[]
}

/** Дедлайн пользователя (GET /api/deadlines). */
export interface Deadline {
  id: number
  subject: string
  teacher: string
  task: string
  /** Срок в ISO или '' — дедлайн без даты. */
  date: string
  /** Дней до срока; null для дедлайна без даты; отрицательное — просрочен. */
  days_left: number | null
}

/** POST /api/deadlines */
export interface DeadlineCreatePayload {
  subject?: string
  task: string
  teacher?: string
  date?: string
}

/** Ответ на создание дедлайна. */
export interface DeadlineCreated {
  ok: true
  item: Deadline
}

/** Роль пользователя (как в students.role + админ из ADMIN_IDS). */
export type Role = 'student' | 'deputy' | 'starosta' | 'admin'

/** GET /api/profile */
export interface Profile {
  tg_id: number
  name: string
  group: string
  role: Role
}

/**
 * Аттестация по предмету.
 *
 * `required` — норматив (MIN_ATTESTATION_LESSONS), `total_lessons` — сколько
 * пар по предмету прошло за месяц. Экран показывает «attended/required» и
 * подпись «из total_lessons возможных».
 */
export interface AttendanceSubject {
  name: string
  attended: number
  required: number
  total_lessons: number
  is_attested: boolean
  need_more: number
}

/** GET /api/attendance */
export interface AttendancePayload {
  present: number
  late: number
  absent: number
  excused: number
  total: number
  /** Доля зачтённых занятий (был + опоздал), 0..100. */
  percent: number
  period_from: string
  period_to: string
  subjects: AttendanceSubject[]
}

/** Идущая пара (GET /api/attendance/active). */
export interface ActiveLessonInfo {
  para: number
  subject: string
  room: string
  time_range: string
  /** Момент закрытия окна ответа (начало пары + 5 минут), ISO. */
  closes_at: string
  answered: 'present' | 'absent' | null
  /** Открыто ли окно ответа прямо сейчас. */
  is_open: boolean
}

/** GET /api/attendance/active */
export interface ActiveLessonPayload {
  active: ActiveLessonInfo | null
}

/** POST /api/attendance/answer */
export interface AttendanceAnswerPayload {
  ok: true
  status: 'present' | 'absent'
}

/** Коды ошибок бэкенда, которые фронт различает. */
export type ApiErrorCode =
  | 'invalid_init_data'
  | 'group_not_set'
  | 'not_in_group'
  | 'bad_date'
  | 'bad_body'
  | 'empty_task'
  | 'task_too_long'
  | 'bad_answer'
  | 'no_active_lesson'
  | 'already_answered'
  | 'poll_closed'
  | 'not_found'