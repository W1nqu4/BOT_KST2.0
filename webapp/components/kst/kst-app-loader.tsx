'use client'

import dynamic from 'next/dynamic'

export const KstAppLoader = dynamic(() => import('./kst-app').then((m) => m.KstApp), {
  ssr: false,
  loading: () => <div className="min-h-dvh" aria-busy="true" />,
})
