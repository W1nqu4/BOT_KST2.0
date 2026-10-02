/** @type {import('next').NextConfig} */
const nextConfig = {
  // Статический экспорт: сборка кладёт готовую SPA в out/, Node.js в runtime не нужен.
  output: 'export',
  // Бот раздаёт Mini App по /app/ (aiohttp add_static), поэтому и страницы,
  // и ассеты должны иметь этот префикс: /app/_next/...
  basePath: '/app',
  // Каждому маршруту — свой каталог index.html: aiohttp отдаёт файл напрямую.
  trailingSlash: true,
  // Оптимизатор картинок требует сервер — в статике он недоступен.
  images: {
    unoptimized: true,
  },
  typescript: {
    ignoreBuildErrors: true,
  },
}

export default nextConfig
