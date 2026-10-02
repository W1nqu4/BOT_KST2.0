import type { Metadata, Viewport } from 'next'
import { Inter } from 'next/font/google'
import Script from 'next/script'
import '@telegram-apps/telegram-ui/dist/styles.css'
import './globals.css'

const inter = Inter({ subsets: ['latin', 'cyrillic'], variable: '--font-inter' })

export const metadata: Metadata = {
  title: 'КСТ — студенческий бот',
  description: 'Расписание, дедлайны и посещаемость студентов Красноярского строительного техникума в Telegram',
  generator: 'v0.app',
  // Next 16 при output: 'export' не добавляет basePath к путям иконок, поэтому
  // префикс /app/ указан явно — иначе браузер запрашивает /icon.svg и получает
  // 404 (файлы лежат в /app/). Путь совпадает с basePath в next.config.mjs.
  icons: {
    icon: [
      {
        url: '/app/icon-light-32x32.png',
        media: '(prefers-color-scheme: light)',
      },
      {
        url: '/app/icon-dark-32x32.png',
        media: '(prefers-color-scheme: dark)',
      },
      {
        url: '/app/icon.svg',
        type: 'image/svg+xml',
      },
    ],
    apple: '/app/apple-icon.png',
  },
}

export const viewport: Viewport = {
  width: 'device-width',
  initialScale: 1,
  maximumScale: 1,
  userScalable: false,
  colorScheme: 'light dark',
  themeColor: [
    { media: '(prefers-color-scheme: light)', color: '#efeff4' },
    { media: '(prefers-color-scheme: dark)', color: '#0f0f0f' },
  ],
}

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode
}>) {
  return (
    <html lang="ru" className={inter.variable} suppressHydrationWarning>
      <head>
        <Script src="https://telegram.org/js/telegram-web-app.js" strategy="beforeInteractive" />
      </head>
      <body className="antialiased">{children}</body>
    </html>
  )
}
