'use client'

import { useEffect, useState } from 'react'

export type Theme = 'light' | 'dark'

/**
 * Тема из Telegram WebApp.
 *
 * `@telegram-apps/telegram-ui` красит свои компоненты через переменные
 * `--tgui--*`, но shadcn-токены (`.dark` в globals.css) сами не переключаются:
 * класс `.dark` на `<html>` никто не ставит. Хук закрывает эту дыру — держит
 * класс и `data-theme` в согласии с `colorScheme` Telegram и слушает
 * `themeChanged` (пользователь может сменить тему не закрывая Web App).
 *
 * Вне Telegram `colorScheme` неизвестен: берём системную схему, чтобы браузер
 * при разработке выглядел как в клиенте.
 */
export function useTheme(): Theme {
  const [theme, setTheme] = useState<Theme>('dark')

  useEffect(() => {
    const webApp = window.Telegram?.WebApp
    const root = document.documentElement

    const apply = () => {
      const scheme: Theme = webApp?.colorScheme
        ? webApp.colorScheme === 'light'
          ? 'light'
          : 'dark'
        : window.matchMedia('(prefers-color-scheme: light)').matches
          ? 'light'
          : 'dark'

      setTheme(scheme)
      root.classList.toggle('dark', scheme === 'dark')
      root.dataset.theme = scheme
    }

    apply()

    // Telegram шлёт themeChanged при смене темы в клиенте.
    webApp?.onEvent?.('themeChanged', apply)
    return () => {
      webApp?.offEvent?.('themeChanged', apply)
    }
  }, [])

  return theme
}