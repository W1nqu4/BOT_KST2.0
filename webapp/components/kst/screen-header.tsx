import { ChevronLeft } from 'lucide-react'

export function ScreenHeader({ title, onBack }: { title: string; onBack?: () => void }) {
  return (
    <header className="sticky top-0 z-20 grid h-12 grid-cols-[1fr_auto_1fr] items-center border-b border-tg-divider bg-tg-bg/90 px-2 backdrop-blur-md">
      <div className="flex justify-start">
        {onBack && (
          <button
            type="button"
            onClick={onBack}
            className="flex items-center gap-0.5 rounded-lg px-1.5 py-1 text-[17px] text-tg-link transition-opacity active:opacity-60"
          >
            <ChevronLeft className="size-6" aria-hidden="true" />
            Назад
          </button>
        )}
      </div>
      <h1 className="truncate text-[17px] font-semibold">{title}</h1>
      <div />
    </header>
  )
}
