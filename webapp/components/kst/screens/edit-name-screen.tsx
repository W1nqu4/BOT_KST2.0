'use client'

import { useState, type FormEvent } from 'react'
import { Button, Input, List, Section } from '@telegram-apps/telegram-ui'

export function EditNameScreen({ initialName, onSubmit }: { initialName: string; onSubmit: (name: string) => void }) {
  const [name, setName] = useState(initialName)
  const trimmed = name.trim()
  const valid = trimmed.length >= 2 && trimmed.length <= 60

  const handleSubmit = (e: FormEvent) => {
    e.preventDefault()
    if (valid) onSubmit(trimmed)
  }

  return (
    <form onSubmit={handleSubmit}>
      <List>
        <Section header="Отображаемое имя" footer="Имя видят староста и преподаватели в отчётах посещаемости">
          <Input
            header="Имя и фамилия"
            placeholder="Иван Петров"
            value={name}
            maxLength={60}
            onChange={(e) => setName(e.target.value)}
            status={valid || name.length === 0 ? 'default' : 'error'}
            autoFocus
          />
        </Section>
        <div className="px-1">
          <Button type="submit" stretched size="l" disabled={!valid}>
            Сохранить
          </Button>
        </div>
      </List>
    </form>
  )
}
