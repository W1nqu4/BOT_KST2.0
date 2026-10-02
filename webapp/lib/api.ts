/**
 * HTTP-клиент Mini App: единственная точка запросов к бэкенду бота.
 *
 * Авторизация — заголовок `X-Telegram-Init-Data` с подписанной строкой
 * Telegram; бэкенд проверяет подпись и достаёт из неё tg_id. Ничего больше
 * передавать не нужно.
 *
 * База URL: в dev задаётся NEXT_PUBLIC_API_BASE (бэкенд на localhost:8080,
 * фронт — на localhost:3000), в проде переменная пустая — API живёт на том же
 * origin, что и страница, поэтому относительные пути работают как есть.
 * rewrites из next.config.mjs использовать нельзя: при `output: 'export'`
 * Next их не поддерживает.
 */

const API_BASE = process.env.NEXT_PUBLIC_API_BASE || ''

/** Ошибка API с HTTP-кодом и машинным кодом из тела ответа. */
export class ApiError extends Error {
  constructor(
    public readonly status: number,
    public readonly code?: string,
  ) {
    super(`API ${status}${code ? `: ${code}` : ''}`)
    this.name = 'ApiError'
  }
}

/** Строка initData из Telegram WebApp ('' вне Telegram). */
export function getInitData(): string {
  if (typeof window === 'undefined') return ''
  return window.Telegram?.WebApp?.initData || ''
}

/** Открыто ли приложение внутри Telegram (есть непустой initData). */
export function isTelegramContext(): boolean {
  return getInitData().length > 0
}

async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  const headers: Record<string, string> = {
    'X-Telegram-Init-Data': getInitData(),
    ...((options.headers as Record<string, string>) || {}),
  }
  if (options.body) headers['Content-Type'] = 'application/json'

  const response = await fetch(API_BASE + path, { ...options, headers })
  if (!response.ok) {
    let code: string | undefined
    try {
      code = (await response.json())?.error
    } catch {
      // Тело не JSON (например, HTML от прокси) — оставляем только статус.
    }
    throw new ApiError(response.status, code)
  }
  return (await response.json()) as T
}

/** GET-запрос к API. */
export function apiGet<T>(path: string): Promise<T> {
  return request<T>(path)
}

/** POST-запрос с JSON-телом. */
export function apiPost<T>(path: string, body: unknown): Promise<T> {
  return request<T>(path, { method: 'POST', body: JSON.stringify(body) })
}

/** DELETE-запрос. */
export function apiDelete<T>(path: string): Promise<T> {
  return request<T>(path, { method: 'DELETE' })
}

/** Человекочитаемое сообщение об ошибке для экранов. */
export function errorMessage(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 401) return 'Откройте приложение из Telegram'
    if (error.code === 'group_not_set') return 'Выберите группу в боте'
    if (error.status >= 500) return 'Сервис недоступен, попробуйте позже'
    return `Ошибка запроса (${error.status})`
  }
  if (error instanceof Error) return error.message
  return 'Не удалось загрузить данные'
}