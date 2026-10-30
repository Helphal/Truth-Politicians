"""Verrou fichier non bloquant portable (Linux fcntl / Windows msvcrt)."""
from __future__ import annotations

import sys
from typing import BinaryIO, TextIO


def try_acquire_exclusive_nonblocking(lock_file: TextIO | BinaryIO) -> bool:
    """Tente un verrou exclusif non bloquant. False si déjà tenu ou indisponible."""
    if sys.platform == "win32":
        import msvcrt

        try:
            lock_file.seek(0)
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False
    try:
        import fcntl
    except ImportError:
        return False
    try:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except BlockingIOError:
        return False
    except OSError:
        return False
