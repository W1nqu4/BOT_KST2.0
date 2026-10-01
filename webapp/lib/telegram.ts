'use client'

import { useEffect, useEffectEvent } from 'react'

type TelegramWebApp = {
  ready: () => void
  expand: () => void
  initData: string
  BackButton: {
    show: () => void
    hide: () => void
    onClick: (cb: () => void) => void
    offClick: (cb: () => void) => void
  }
  HapticFeedback?: {
    selectionChanged: () => void
    impactOccurred: (style: 'light' | 'medium' | 'heavy') => void
  }
  showAlert?: (message: string) => void
}

declare global {
  interface Window {
    Telegram?: { WebApp?: TelegramWebApp }
  }
}

export function getWebApp(): TelegramWebApp | undefined {
  if (typeof window === 'undefined') return undefined
  const webApp = window.Telegram?.WebApp
  return webApp && webApp.initData ? webApp : undefined
}

export function haptic() {
  getWebApp()?.HapticFeedback?.selectionChanged()
}

export function notify(message: string) {
  const webApp = getWebApp()
  if (webApp?.showAlert) webApp.showAlert(message)
  else window.alert(message)
}

export function useTelegramInit() {
  useEffect(() => {
    const webApp = getWebApp()
    webApp?.ready()
    webApp?.expand()
  }, [])
}

export function useTelegramBackButton(visible: boolean, onBack: () => void) {
  const handleBack = useEffectEvent(onBack)

  useEffect(() => {
    const webApp = getWebApp()
    if (!webApp) return
    const listener = () => handleBack()
    if (visible) {
      webApp.BackButton.show()
      webApp.BackButton.onClick(listener)
    } else {
      webApp.BackButton.hide()
    }
    return () => webApp.BackButton.offClick(listener)
  }, [visible])
}
