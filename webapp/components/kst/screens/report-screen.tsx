import { List, Section } from '@telegram-apps/telegram-ui'
import { CircleAlert, CircleCheck, CircleX } from 'lucide-react'
import { STUDENT, WEEK_REPORT, plural } from '@/lib/kst-data'

export function ReportScreen() {
  const { range, atRisk, truants, excellent } = WEEK_REPORT

  return (
    <List>
      <section className="grid grid-cols-3 gap-2 px-1 pt-2">
        <Summary value={atRisk.length} label="Под угрозой" className="text-tg-destructive" />
        <Summary value={truants.length} label="С прогулами" className="text-tg-warning" />
        <Summary value={excellent.length} label="Отлично" className="text-tg-success" />
      </section>

      <Section header={`${STUDENT.group} · ${range}`}>
        <Group icon={<CircleAlert className="size-5 text-tg-destructive" aria-hidden="true" />} title="Под угрозой неаттестации">
          {atRisk.map((s) => (
            <Row key={s.name} name={s.name}>
              <span className="flex flex-wrap gap-1">
                {s.items.map((item) => (
                  <span key={item} className="rounded-md bg-tg-destructive/15 px-1.5 py-0.5 text-xs font-medium text-tg-destructive">
                    {item}
                  </span>
                ))}
              </span>
            </Row>
          ))}
        </Group>
      </Section>

      <Section>
        <Group icon={<CircleX className="size-5 text-tg-warning" aria-hidden="true" />} title="Прогулы">
          {truants.map((s) => (
            <Row key={s.name} name={s.name}>
              <span className="text-sm text-tg-hint tabular-nums">
                {s.count} {plural(s.count, 'пара', 'пары', 'пар')}
              </span>
            </Row>
          ))}
        </Group>
      </Section>

      <Section footer="Студенты с 3 и более прогулами получат напоминание в воскресенье в 19:00.">
        <Group icon={<CircleCheck className="size-5 text-tg-success" aria-hidden="true" />} title="Отличная посещаемость">
          {excellent.map((name) => (
            <Row key={name} name={name}>
              <span className="text-sm text-tg-success">Все предметы</span>
            </Row>
          ))}
        </Group>
      </Section>
    </List>
  )
}

function Summary({ value, label, className }: { value: number; label: string; className: string }) {
  return (
    <div className="flex flex-col items-center rounded-xl bg-tg-section px-2 py-3">
      <span className={`text-2xl font-bold tabular-nums ${className}`}>{value}</span>
      <span className="text-xs text-tg-hint">{label}</span>
    </div>
  )
}

function Group({ icon, title, children }: { icon: React.ReactNode; title: string; children: React.ReactNode }) {
  return (
    <div>
      <div className="flex items-center gap-2 px-4 pt-3 pb-1">
        {icon}
        <h2 className="text-[15px] font-semibold">{title}</h2>
      </div>
      <ul>{children}</ul>
    </div>
  )
}

function Row({ name, children }: { name: string; children: React.ReactNode }) {
  return (
    <li className="flex items-center justify-between gap-3 border-b border-tg-divider px-4 py-2.5 last:border-b-0">
      <span className="text-[15px]">{name}</span>
      {children}
    </li>
  )
}
