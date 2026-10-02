'use client'

import { Button, Placeholder } from '@telegram-apps/telegram-ui'
import { CircleAlert, RefreshCw } from 'lucide-react'

import { cn } from '@/lib/utils'

/**
 * Состояния загрузки и ошибки для экранов Mini App.
 *
 * Вынесены отдельно, чтобы каждый экран не рисовал их по-своему: одинаковые
 * скелетоны и одинаковая кнопка «Обновить» — часть общего вида приложения.
 */

/** Скелетон списка: прямоугольники вместо контента, пока идёт запрос. */
export function SkeletonList({ rows = 3, className }: { rows?: number; className?: string }) {
  return (
    <div className={cn('flex flex-col gap-3 px-4 pt-4', className)} aria-busy="true" aria-live="polite">
      <span className="sr-only">Загрузка</span>
      {Array.from({ length: rows }).map((_, index) => (
        <div key={index} className="h-24 animate-pulse rounded-2xl bg-tg-section" />
      ))}
    </div>
  )
}

/** Скелетон «шапки» экрана (карточка дня, сводка) — для главной и явки. */
export function SkeletonHero({ className }: { className?: string }) {
  return (
    <div className={cn('flex flex-col gap-3 px-4 pt-4', className)} aria-busy="true">
      <div className="h-40 animate-pulse rounded-3xl bg-tg-section" />
      <div className="h-16 animate-pulse rounded-2xl bg-tg-section" />
      <div className="h-24 animate-pulse rounded-2xl bg-tg-section" />
    </div>
  )
}

/** Ошибка загрузки с кнопкой повтора. */
export function ErrorState({
  message,
  onRetry,
  action,
}: {
  message: string
  onRetry?: () => void
  /** Дополнительное действие (например, кнопка перехода в бота). */
  action?: { label: string; onClick: () => void }
}) {
  return (
    <div className="flex flex-col gap-3 px-4 pt-6">
      <Placeholder
        header="Не удалось загрузить"
        description={message}
        action={onRetry ? { children: 'Обновить', onClick: onRetry } : undefined}
      />
      {onRetry && (
        <Button
          stretched
          size="l"
          mode="bezeled"
          before={<RefreshCw className="size-5" aria-hidden="true" />}
          onClick={onRetry}
        >
          Обновить
        </Button>
      )}
      {action && (
        <Button stretched size="l" mode="plain" before={<CircleAlert className="size-5" aria-hidden="true" />} onClick={action.onClick}>
          {action.label}
        </Button>
      )}
    </div>
  )
}

/** Пустое состояние экрана (нет данных, но запрос успешен). */
export function EmptyState({ header, description }: { header: string; description?: string }) {
  return <Placeholder header={header} description={description} />
}