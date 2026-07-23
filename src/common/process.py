import os


def is_process_alive(pid: int | None) -> bool:
    """判断本机进程是否仍存在。"""
    if pid is None or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True
