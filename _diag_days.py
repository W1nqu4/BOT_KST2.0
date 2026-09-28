"""Временная диагностика: воспроизведение симптома Вт/Ср."""
import sqlite3
import sys
from datetime import date

sys.stdout.reconfigure(encoding="utf-8")

from bot.services.schedule_service import (
    apply_substitutions,
    get_lessons_for_day,
    week_type_for_date,
)

conn = sqlite3.connect("data/bot.db")
conn.row_factory = sqlite3.Row

GROUP = "26КАД"

for d in [date(2026, 9, 28), date(2026, 9, 29), date(2026, 9, 30),
          date(2026, 10, 1), date(2026, 10, 2), date(2026, 10, 3)]:
    base = get_lessons_for_day(conn, GROUP, d)
    after = apply_substitutions(conn, base, GROUP, d)
    print(f"{d} iso={d.isoweekday()} parity={week_type_for_date(d)!r} "
          f"base={len(base)} after_subs={len(after)}")

print()
print("--- detail 29.09 (Вт) ---")
for lesson in apply_substitutions(conn, get_lessons_for_day(conn, GROUP, date(2026, 9, 29)), GROUP, date(2026, 9, 29)):
    print("  ", lesson["para_number"], lesson["subject"], "|", lesson["teacher"], "|", lesson["room"], "|", lesson["week_type"])

print("--- detail 30.09 (Ср) ---")
for lesson in apply_substitutions(conn, get_lessons_for_day(conn, GROUP, date(2026, 9, 30)), GROUP, date(2026, 9, 30)):
    print("  ", lesson["para_number"], lesson["subject"], "|", lesson["teacher"], "|", lesson["room"], "|", lesson["week_type"])

print()
print("--- users ---")
for row in conn.execute("SELECT tg_id, group_name, is_active FROM users"):
    print("  ", dict(row))

print()
print("--- substitutions table columns ---")
cols = conn.execute("PRAGMA table_info(substitutions_cache)").fetchall()
print("  ", [c[1] for c in cols])
print("--- substitutions distinct dates ---")
print("  ", conn.execute("SELECT DISTINCT date FROM substitutions_cache ORDER BY date").fetchall())
print("--- subs for 26КАД 29.09 ---")
for row in conn.execute("SELECT * FROM substitutions_cache WHERE group_name=? AND date=?", (GROUP, "2026-09-29")):
    print("  ", dict(row))