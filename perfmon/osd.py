"""Вывод строки внутрь игры через OSD-слот RTSS.

Обычное окно поверх остальных не видно в полноэкранной игре: в exclusive
fullscreen приложение владеет экраном целиком, и никакой Z-порядок не поможет.
Единственный способ нарисовать что-то внутри такой игры -- отдать это тому, кто
уже внедрён в её процесс. RTSS ровно для этого и существует.

Механика документирована самим RTSS: в общей памяти есть массив OSD-слотов, у
каждого поле владельца и поле текста. Клиент занимает свободный слот, пишет в
него свой текст и увеличивает счётчик кадров OSD в заголовке -- RTSS
перерисовывает оверлей во всех активных 3D-приложениях.

    "You should change this field only if it is empty (i.e. when OSD slot is
    not owned by any application) or if it is set to your own application's ID"

Поэтому чужие слоты (например, слот Afterburner) не трогаются никогда.
"""

from __future__ import annotations

import ctypes
import os

from .rtss import HDR_APP_ENTRY_SIZE, HDR_SIGNATURE, SHARED_MEMORY_NAME, SIGNATURES, FpsStats
from .shmem import SharedMemory
from .sources import Snapshot

# --- заголовок RTSS_SHARED_MEMORY, часть про OSD ---
HDR_OSD_ENTRY_SIZE = 0x14
HDR_OSD_ARR_OFFSET = 0x18
HDR_OSD_ARR_SIZE = 0x1C
HDR_OSD_FRAME = 0x20

# --- RTSS_SHARED_MEMORY_OSD_ENTRY ---
OSD_TEXT = 0x000          # char szOSD[256]
OSD_TEXT_SIZE = 256
OSD_OWNER = 0x100         # char szOSDOwner[256]
OSD_OWNER_SIZE = 256
OSD_TEXT_EX = 0x200       # char szOSDEx[4096], появился в v2.7
OSD_TEXT_EX_SIZE = 4096
OSD_ENTRY_V27 = OSD_TEXT_EX + OSD_TEXT_EX_SIZE

# В имени владельца несём свой PID. Штатный выход слот освобождает, но при
# аварийном завершении освобождать некому, и строка зависла бы во всех играх до
# перезапуска RTSS. По PID видно, что владелец давно мёртв, и слот можно забрать.
OWNER_PREFIX = "PerfMonOverlay"
OWNER_ID = f"{OWNER_PREFIX}:{os.getpid()}"

_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_STILL_ACTIVE = 259
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)


def _process_alive(pid: int) -> bool:
    """Жив ли процесс.

    Одного успешного ``OpenProcess`` мало: пока кто-то держит хэндл, объект
    завершившегося процесса остаётся в ядре и открывается как ни в чём не
    бывало. Отличает живого от мёртвого только код выхода.
    """
    handle = _kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    code = ctypes.c_ulong(0)
    ok = _kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
    _kernel32.CloseHandle(handle)
    return bool(ok) and code.value == _STILL_ACTIVE


def _is_abandoned(owner: str) -> bool:
    """Слот наш, но от процесса, которого уже нет."""
    if owner == OWNER_PREFIX:
        return True     # имя без PID -- от старой сборки, владельца не проверить
    if not owner.startswith(f"{OWNER_PREFIX}:"):
        return False
    try:
        pid = int(owner.split(":", 1)[1])
    except ValueError:
        return True
    return not _process_alive(pid)

# Те же цвета, что и в оконной строке
COLOR_FPS = "FFC83D"
COLOR_CPU = "62C2FF"
COLOR_GPU = "7CE38B"
COLOR_RAM = "D48EFF"
COLOR_DIM = "A5A5A5"


def _number(value: float | None, digits: int = 0) -> str:
    return "--" if value is None else f"{value:.{digits}f}"


def format_line(snapshot: Snapshot, fps: FpsStats, rich: bool = True) -> str:
    """Собрать строку для OSD.

    Текст, а не картинка: RTSS рисует свой оверлей собственным движком, и
    попиксельную графику оконной строки туда не передать. Поэтому графики
    заменены диапазонами «минимум..максимум», а разметка -- цветовыми тегами
    RTSS вида ``<C=RRGGBB>``.

    Единицы записаны латиницей и знаком градуса: поля OSD однобайтовые, и
    кириллица в них не переживёт кодировку.
    """
    def tag(text: str, color: str) -> str:
        return f"<C={color}>{text}<C>" if rich else text

    cpu, gpu, ram = snapshot.cpu, snapshot.gpu, snapshot.ram
    parts = [
        f"{tag('FPS', COLOR_FPS)} {_number(fps.current)}"
        f" {tag(f'{_number(fps.minimum)}..{_number(fps.maximum)}', COLOR_DIM)}",

        f"{tag('CPU', COLOR_CPU)} {_number(cpu.load_avg)}..{_number(cpu.load_max)}%"
        f" {_number(cpu.clock_avg, 2)}..{_number(cpu.clock_max, 2)}GHz"
        f" {_number(cpu.temp)}°C",

        f"{tag('GPU', COLOR_GPU)} {_number(gpu.load)}%"
        f" {_number(gpu.temp)}°C"
        f" {_number(gpu.vram_used, 1)}/{_number(gpu.vram_total, 1)}GB",

        f"{tag('RAM', COLOR_RAM)} {_number(ram.used, 1)}/{_number(ram.total, 1)}GB",
    ]
    return "  |  ".join(parts)


class RtssOsd:
    """Занятый в RTSS слот, в который пишется наша строка."""

    def __init__(self) -> None:
        self._shm: SharedMemory | None = None
        self._slot: int | None = None
        self._entry_size = 0
        self._arr_offset = 0
        self._arr_size = 0
        self._last_text = ""
        self.error: str | None = None
        self._open()

    def _open(self) -> bool:
        try:
            shm = SharedMemory(SHARED_MEMORY_NAME, writable=True)
        except OSError as exc:
            # Чаще всего это отказ в доступе: RTSS запущен от администратора,
            # а монитор -- нет. Читать по-прежнему можно, писать в OSD -- нет.
            self.error = str(exc)
            return False
        if shm.u32(HDR_SIGNATURE) not in SIGNATURES:
            shm.close()
            self.error = "подпись RTSSSharedMemoryV2 не совпала"
            return False

        self._shm = shm
        self._entry_size = shm.u32(HDR_OSD_ENTRY_SIZE)
        self._arr_offset = shm.u32(HDR_OSD_ARR_OFFSET)
        self._arr_size = shm.u32(HDR_OSD_ARR_SIZE)
        if not (self._entry_size and self._arr_size):
            self.close()
            self.error = "в общей памяти RTSS нет массива OSD-слотов"
            return False
        # Размер записи приложения читаем только чтобы убедиться, что заголовок
        # разобран верно: у пустой или чужой области он был бы нулевым
        self.error = None if shm.u32(HDR_APP_ENTRY_SIZE) else "заголовок пуст"
        return self.error is None

    @property
    def available(self) -> bool:
        return self._shm is not None

    @property
    def supports_tags(self) -> bool:
        """Цветовые теги живут в szOSDEx, а он появился только в v2.7."""
        return self._entry_size >= OSD_ENTRY_V27

    def _wipe(self, index: int) -> None:
        shm = self._shm
        if shm is None:
            return
        base = self._arr_offset + index * self._entry_size
        shm.set_text(base + OSD_TEXT, OSD_TEXT_SIZE, "")
        if self.supports_tags:
            shm.set_text(base + OSD_TEXT_EX, OSD_TEXT_EX_SIZE, "")
        shm.set_text(base + OSD_OWNER, OSD_OWNER_SIZE, "")

    def _claim(self) -> int | None:
        """Найти свой слот, свободный или брошенный прошлым запуском.

        Массив просматривается целиком: брошенных слотов может накопиться
        несколько (каждое аварийное завершение оставляет свой), и все их надо
        очистить, иначе в игре будет висеть стопка устаревших строк.
        """
        shm = self._shm
        if shm is None:
            return None
        mine: int | None = None
        free: int | None = None
        for index in range(self._arr_size):
            base = self._arr_offset + index * self._entry_size
            owner = shm.text(base + OSD_OWNER, OSD_OWNER_SIZE)
            if owner == OWNER_ID:
                mine = index if mine is None else mine
            elif _is_abandoned(owner):
                self._wipe(index)
                if free is None:
                    free = index
            elif not owner and free is None:
                free = index

        index = mine if mine is not None else free
        if index is not None:
            base = self._arr_offset + index * self._entry_size
            shm.set_text(base + OSD_OWNER, OSD_OWNER_SIZE, OWNER_ID)
        return index

    def update(self, text: str) -> bool:
        """Показать строку в игре. Пустой текст очищает слот."""
        shm = self._shm
        if shm is None:
            return False
        if self._slot is None:
            self._slot = self._claim()
            if self._slot is None:
                self.error = "все OSD-слоты RTSS заняты"
                return False
        if text == self._last_text:
            return True

        base = self._arr_offset + self._slot * self._entry_size
        if self.supports_tags:
            # RTSS показывает szOSDEx, если он не пуст; szOSD оставляем пустым,
            # иначе строка нарисуется дважды
            shm.set_text(base + OSD_TEXT, OSD_TEXT_SIZE, "")
            shm.set_text(base + OSD_TEXT_EX, OSD_TEXT_EX_SIZE, text)
        else:
            shm.set_text(base + OSD_TEXT, OSD_TEXT_SIZE, text)
        # Счётчик кадров OSD в заголовке -- сигнал серверу перерисовать оверлей
        shm.set_u32(HDR_OSD_FRAME, shm.u32(HDR_OSD_FRAME) + 1)
        self._last_text = text
        return True

    def release(self) -> None:
        """Освободить слот: иначе строка останется висеть в играх после выхода."""
        shm = self._shm
        if shm is None or self._slot is None:
            return
        base = self._arr_offset + self._slot * self._entry_size
        shm.set_text(base + OSD_TEXT, OSD_TEXT_SIZE, "")
        if self.supports_tags:
            shm.set_text(base + OSD_TEXT_EX, OSD_TEXT_EX_SIZE, "")
        shm.set_text(base + OSD_OWNER, OSD_OWNER_SIZE, "")
        shm.set_u32(HDR_OSD_FRAME, shm.u32(HDR_OSD_FRAME) + 1)
        self._slot = None
        self._last_text = ""

    def close(self) -> None:
        try:
            self.release()
        except Exception:
            pass
        if self._shm is not None:
            self._shm.close()
            self._shm = None
