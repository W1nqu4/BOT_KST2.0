import { Ban, BookOpen, NotebookPen, Repeat2, type LucideIcon } from 'lucide-react'
import type { LessonStatus } from '@/lib/kst-data'
import { cn } from '@/lib/utils'

export const STATUS_META: Record<
  LessonStatus,
  { label: string; icon: LucideIcon; className: string; textClassName: string }
> = {
  plan: { label: 'По плану', icon: BookOpen, className: 'bg-tg-fill text-tg-link', textClassName: 'text-tg-link' },
  replace: {
    label: 'Замена',
    icon: Repeat2,
    className: 'bg-tg-warning/15 text-tg-warning',
    textClassName: 'text-tg-warning',
  },
  cancel: {
    label: 'Отмена',
    icon: Ban,
    className: 'bg-tg-destructive/15 text-tg-destructive',
    textClassName: 'text-tg-destructive',
  },
  self: {
    label: 'Самостоятельная',
    icon: NotebookPen,
    className: 'bg-tg-success/15 text-tg-success',
    textClassName: 'text-tg-success',
  },
}

export function LessonStatusIcon({ status, className }: { status: LessonStatus; className?: string }) {
  const meta = STATUS_META[status]
  const Icon = meta.icon
  return (
    <span
      className={cn('flex size-10 shrink-0 items-center justify-center rounded-xl', meta.className, className)}
      title={meta.label}
    >
      <Icon className="size-5" aria-hidden="true" />
      <span className="sr-only">{meta.label}</span>
    </span>
  )
}

export function LessonStatusChip({ status }: { status: LessonStatus }) {
  const meta = STATUS_META[status]
  const Icon = meta.icon
  return (
    <span className={cn('inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-xs font-medium', meta.className)}>
      <Icon className="size-3.5" aria-hidden="true" />
      {meta.label}
    </span>
  )
}
