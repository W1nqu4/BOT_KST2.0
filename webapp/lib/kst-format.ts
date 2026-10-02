/**
 * Форматирование дат и чисел для Mini App.
 *
 * Моки из `lib/kst-data.ts` удалены: расписание и дедлайны приходят с бэкенда.
 * Здесь остаётся только то, что нужно для отображения — расчёт дат, подписи
 * дней недели и русские склонения. Даты считаются в UTC, чтобы переход на
 * летнее время не сдвигал день.
 */

const MONTHS_SHORT = ['янв', 'фев', 'мар', 'апр', 'мая', 'июн', 'июл', 'авг', 'сен', 'окт', 'ноя', 'дек']
const MONTHS_LONG = [
  'января', 'февраля', 'марта', 'апреля', 'мая', 'июня',
  'июля', 'августа', 'сентября', 'октября', 'ноября', 'декабря',
]
const WEEKDAYS_SHORT = ['Пн', 'Вт', 'Ср', 'Чт', 'Пт', 'Сб', 'Вс']
const WEEKDAYS_LONG = ['Понедельник', 'Вторник', 'Среда', 'Четверг', 'Пятница', 'Суббота', 'Воскресенье']

/** Сегодняшняя дата в формате YYYY-MM-DD (по местному времени устройства). */
export function todayISO(): string {
  const now = new Date()
  return toISO(now.getFullYear(), now.getMonth() + 1, now.getDate())
}

/** Собрать ISO-дату из числовых компонентов. */
export function toISO(year: number, month: number, day: number): string {
  return `${year}-${String(month).padStart(2, '0')}-${String(day).padStart(2, '0')}`
}

/** Прибавить дни к ISO-дате. */
export function addDaysISO(iso: string, days: number): string {
  const date = parseISO(iso)
  date.setUTCDate(date.getUTCDate() + days)
  return date.toISOString().slice(0, 10)
}

/** Разобрать ISO-дату как полночь UTC (без сдвигов пояса). */
export function parseISO(iso: string): Date {
  return new Date(`${iso}T00:00:00Z`)
}

/** Понедельник недели, в которую попадает дата. */
export function weekStartISO(iso: string): string {
  const date = parseISO(iso)
  // getUTCDay: 0 — воскресенье, 1 — понедельник.
  const offset = (date.getUTCDay() + 6) % 7
  return addDaysISO(iso, -offset)
}

/** Компоненты даты для карточек и шапок. */
export function dateParts(iso: string) {
  const date = parseISO(iso)
  const index = date.getUTCDay()
  return {
    day: date.getUTCDate(),
    month: MONTHS_SHORT[date.getUTCMonth()],
    /** «Ср» */
    weekdayShort: WEEKDAYS_SHORT[(index + 6) % 7],
    /** «Среда, 7 октября» */
    long: `${WEEKDAYS_LONG[(index + 6) % 7]}, ${date.getUTCDate()} ${MONTHS_LONG[date.getUTCMonth()]}`,
  }
}

/** Шесть учебных дней (Пн–Сб) недели, в которую попадает дата. */
export function weekdaysFrom(iso: string) {
  const monday = weekStartISO(iso)
  return Array.from({ length: 6 }).map((_, offset) => {
    const dayISO = addDaysISO(monday, offset)
    const date = parseISO(dayISO)
    return {
      iso: dayISO,
      day: date.getUTCDate(),
      short: WEEKDAYS_SHORT[offset],
      full: WEEKDAYS_LONG[offset],
    }
  })
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

/** «через 3 дня», «сегодня», «просрочено на 2 дня» — для дедлайнов. */
export function relativeLabel(daysLeft: number | null): string {
  if (daysLeft === null) return 'Без срока'
  if (daysLeft === 0) return 'Сегодня'
  if (daysLeft === 1) return 'Завтра'
  if (daysLeft === 2) return 'Послезавтра'
  if (daysLeft < 0) {
    return `Просрочено на ${-daysLeft} ${plural(-daysLeft, 'день', 'дня', 'дней')}`
  }
  return `Через ${daysLeft} ${plural(daysLeft, 'день', 'дня', 'дней')}`
}