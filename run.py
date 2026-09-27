"""Локальный запуск: ``python run.py``.

Альтернатива ``python -m bot.main`` — удобно в контейнере и при отладке.
"""

from bot.main import main

if __name__ == "__main__":
    main()