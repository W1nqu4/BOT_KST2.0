'use client'

import { useCallback, useEffect, useState } from 'react'

import { apiGet, errorMessage } from './api'

/** Результат запроса: данные, состояния и ручное обновление. */
export type ApiState<T> = {
  data: T | null
  loading: boolean
  error: string | null
  /** Код ошибки от бэкенда (например, ``group_not_set``) — для спец. экранов. */
  errorCode: string | null
  refresh: () => void
}

/**
 * Загрузить данные по GET-пути.
 *
 * `path = null` — запрос не нужен (например, экран ещё не знает дату):
 * хук остаётся в состоянии «не загружается, данных нет».
 *
 * Отмена через `cancelled` обязательна: при быстром переключении экранов
 * ответ старого запроса не должен перезаписать данные нового.
 */
export function useApi<T>(path: string | null): ApiState<T> {
  const [data, setData] = useState<T | null>(null)
  const [loading, setLoading] = useState(Boolean(path))
  const [error, setError] = useState<string | null>(null)
  const [errorCode, setErrorCode] = useState<string | null>(null)
  const [refreshKey, setRefreshKey] = useState(0)

  useEffect(() => {
    if (!path) {
      setLoading(false)
      return
    }

    let cancelled = false
    setLoading(true)
    setError(null)
    setErrorCode(null)

    apiGet<T>(path)
      .then((payload) => {
        if (!cancelled) setData(payload)
      })
      .catch((cause: unknown) => {
        if (cancelled) return
        setError(errorMessage(cause))
        setErrorCode(
          cause && typeof cause === 'object' && 'code' in cause
            ? ((cause as { code?: string }).code ?? null)
            : null,
        )
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })

    return () => {
      cancelled = true
    }
  }, [path, refreshKey])

  const refresh = useCallback(() => setRefreshKey((key) => key + 1), [])

  return { data, loading, error, errorCode, refresh }
}