"""Child processes the TUI kills when it quits: Textual waits for its worker threads, and they wait on these."""

import os
import signal
import subprocess
import threading

RUNNING = set()
LOCK = threading.Lock()


def start(cmd, **kwargs):
    """Popen in its own process group, registered under the lock stop_all takes, so stop_all can't miss it."""
    with LOCK:
        proc = subprocess.Popen(cmd, start_new_session=True, **kwargs)
        RUNNING.add(proc)
        if worker_cancelled():  # the app quit after stop_all took its list (say the next ffmpeg): it dies too
            kill_group(proc.pid)
    return proc


def untrack(proc):
    with LOCK:
        RUNNING.discard(proc)


def worker_cancelled():
    """Whether this runs in a Textual worker that was cancelled; the TUI cancels them all before stop_all."""
    from textual.worker import NoActiveWorker, get_current_worker
    try:
        return get_current_worker().is_cancelled
    except NoActiveWorker:  # the CLI, or a plain thread
        return False


def kill_group(pgid, sig=signal.SIGTERM):
    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError):  # macOS: EPERM when the group is only an unreaped zombie
        pass


def stop_all():
    """Kill every tracked process and its children (each runs in its own process group)."""
    with LOCK:
        procs = list(RUNNING)
    for proc in procs:
        kill_group(proc.pid)


def run(cmd, timeout):
    """subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=timeout), killable by stop_all()."""
    with start(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as proc:
        try:
            out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            kill_group(proc.pid, signal.SIGKILL)
            proc.communicate()
            raise
        finally:
            untrack(proc)
    if proc.returncode:
        raise subprocess.CalledProcessError(proc.returncode, cmd, out, err)
    return out
