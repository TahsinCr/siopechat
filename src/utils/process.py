import os
from pathlib import Path


def set_private_permissions(path: Path, mode: int) -> None:
    """Use POSIX mode bits where supported; Windows inherits its directory ACL."""
    if os.name != "nt":
        os.chmod(path, mode)


def detached_process_options() -> dict[str, int | bool]:
    """Return subprocess flags for a background process on the current OS."""
    if os.name == "nt":
        return {"creationflags": 0x00000008 | 0x00000200}  # DETACHED_PROCESS | NEW_PROCESS_GROUP
    return {"start_new_session": True}


def acquire_exclusive(lock_file) -> None:
    """Acquire a non-blocking process lock using the host's standard library."""
    if os.name == "nt":
        import msvcrt

        lock_file.seek(0, os.SEEK_END)
        if lock_file.tell() == 0:
            lock_file.write("\0")
            lock_file.flush()
        lock_file.seek(0)
        try:
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as error:
            raise BlockingIOError from error
        return

    import fcntl

    fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)


def configure_memory_allocator() -> None:
    import sys

    if sys.platform.startswith("linux"):
        try:
            import ctypes

            libc = ctypes.CDLL("libc.so.6")
            if hasattr(libc, "mallopt"):
                libc.mallopt(-8, 2)
        except Exception:
            pass


def trim_memory() -> None:
    import gc
    import sys

    try:
        gc.collect()
    except Exception:
        pass
    if sys.platform.startswith("linux"):
        try:
            import ctypes

            libc = ctypes.CDLL("libc.so.6")
            if hasattr(libc, "malloc_trim"):
                libc.malloc_trim(0)
        except Exception:
            pass
    elif sys.platform == "win32":
        try:
            import ctypes

            handle = ctypes.windll.kernel32.GetCurrentProcess()
            ctypes.windll.kernel32.SetProcessWorkingSetSize(handle, -1, -1)
        except Exception:
            pass
    elif sys.platform == "darwin":
        try:
            import ctypes

            libc = ctypes.CDLL("libSystem.B.dylib")
            if hasattr(libc, "malloc_zone_pressure_relief"):
                libc.malloc_zone_pressure_relief(0, 0)
        except Exception:
            pass
