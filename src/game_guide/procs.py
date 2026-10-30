"""Child processes the TUI kills when it quits: Textual waits for its worker threads, and they wait on these."""

import os
import signal
import subprocess
import sys
import threading

WIN = sys.platform == "win32"
# POSIX: each child leads its own process group, so one signal reaches it and everything it spawned. Windows has
# neither Popen's flag nor killpg; `taskkill /T` is what reaches the whole tree there.
GROUP = {} if WIN else {"start_new_session": True}
RUNNING = set()
LOCK = threading.Lock()


def start(cmd, **kwargs):
    """Popen in its own process group (POSIX), registered under the lock stop_all takes, so stop_all can't miss it."""
    with LOCK:
        proc = subprocess.Popen(cmd, **{**GROUP, **kwargs})
        RUNNING.add(proc)
        if worker_cancelled():  # the app quit after stop_all took its list (say the next ffmpeg): it dies too
            kill_tree(proc.pid)
    return proc


def untrack(proc):
    with LOCK:
        RUNNING.discard(proc)


def worker_cancelled():
    """Whether this runs in a Textual worker that is cancelled; the TUI cancels them all before stop_all."""
    from textual.worker import NoActiveWorker, get_current_worker
    try:
        return get_current_worker().is_cancelled
    except NoActiveWorker:  # the CLI, or a plain thread
        return False


def kill_tree(pid, sig=None, *, force=False):
    """Kill a process and everything it spawned (POSIX: the process group; Windows: `taskkill /T`).

    `force` is the second step after SIGTERM is ignored. It must not look up `signal.SIGKILL` on Windows,
    where that name does not exist; `taskkill /F` is already a force kill.
    """
    if WIN:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
        return
    if sig is None:
        sig = signal.SIGKILL if force else signal.SIGTERM
    try:
        os.killpg(pid, sig)
    except (ProcessLookupError, PermissionError):  # macOS: EPERM when the group is only an unreaped zombie
        pass


def stop_all():
    """Kill every tracked process and its children."""
    with LOCK:
        procs = list(RUNNING)
    for proc in procs:
        kill_tree(proc.pid)


def run(cmd, timeout):
    """subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=timeout), killable by stop_all()."""
    with start(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as proc:
        try:
            out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            kill_tree(proc.pid, force=True)
            proc.communicate()
            raise
        finally:
            untrack(proc)
    if proc.returncode:
        raise subprocess.CalledProcessError(proc.returncode, cmd, out, err)
    return out
