"""Запуск оверлея мониторинга производительности.

    python main.py                 # обычный запуск
    python main.py --fps demo      # посмотреть внешний вид без PresentMon
    python main.py --help          # все ключи
"""

import sys

from perfmon.app import run

if __name__ == "__main__":
    sys.exit(run())
