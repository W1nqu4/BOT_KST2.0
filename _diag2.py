"""Временная диагностика: рендер всех групп/дней + замены."""
import sqlite3
import sys
from datetime import date, timedelta

sys.stdout.reconfigure(encoding="utf-8")

from bot.handlers.schedule import render_day
from bot.services.schedule_service import (
    apply_substitutions,
    get_lessons_for_day,
    week_type_for_date,
)

conn = sqlite3.connect("data/bot.db")
conn.row_factory = sqlite3.Row

print("--- substitutions_cache ---")
print("cols:", [c[1] for c in conn.execute("PRAGMA table_info(substitutions_cache)")])
print("dates:", conn.execute(
    "SELECT date_iso, COUNT(*) FROM substitutions_cache GROUP BY date_iso"
).fetchall())
print("26КАД subs 28.09:", conn.execute(
    "SELECT para, old_subject, new_subject, teacher, room, is_cancelled, is_self_study"
    " FROM substitutions_cache WHERE group_name='26КАД' AND date_iso='2026-09-28'"
).fetchall())

print()
print("--- render всех групп/дней ---")
groups = [r[0] for r in conn.execute(
    "SELECT DISTINCT group_name FROM schedule_cache ORDER BY group_name"
)]
errors, empties = [], []
for group in groups:
    for offset in range(0, 7):
        d = date(2026, 9, 28) + timedelta(days=offset)
        try:
            lessons = apply_substitutions(
                conn, get_lessons_for_day(conn, group, d), group, d
            )
            render_day(group, d, lessons)
            if not lessons:
                empties.append((group, d.isoformat()))
        except Exception as exc:
            errors.append((group, d.isoformat(), repr(exc)))
print("groups:", len(groups), "render errors:", len(errors))
for e in errors[:10]:
    print("  ERR", e)
print("empty (group,date) count:", len(empties), "sample:", empties[:8])

print()
print("--- 25КАД (реальный tg 908084777) по дням ---")
for offset in range(0, 7):
    d = date(2026, 9, 28) + timedelta(days=offset)
    lessons = apply_substitutions(
        conn, get_lessons_for_day(conn, "25КАД", d), "25КАД", d
    )
    print(f"  {d} {d.isoweekday()} parity={week_type_for_date(d)!r} -> {len(lessons)}")

print()
print("--- render_day 29.09 26КАД ---")
d = date(2026, 9, 29)
print(render_day("26КАД", d, apply_substitutions(
    conn, get_lessons_for_day(conn, "26КАД", d), "26КАД", d
)))