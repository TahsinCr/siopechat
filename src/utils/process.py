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
