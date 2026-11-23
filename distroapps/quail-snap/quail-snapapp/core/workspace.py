import atexit
import fcntl
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time

from core import mounts

DEFAULT_ROOT = "/home/quail_snapwork"
LOCK_NAME = ".quail-snap-build.lock"

_workspaces = []
_registered = False
_failed = False
_original_excepthook = None


class WorkspaceError(Exception):
    pass


def _on_unhandled_exception(exc_type, exc_value, exc_tb):
    global _failed
    if exc_type is not KeyboardInterrupt:
        _failed = True
    if _original_excepthook is not None:
        _original_excepthook(exc_type, exc_value, exc_tb)


def _release_mounts_under(path, mounts_path="/proc/mounts"):
    root = os.path.realpath(path)
    targets = [os.path.realpath(e["mountpoint"]) for e in mounts.read_mounts(mounts_path)]
    targets = [mp for mp in targets if mp == root or mp.startswith(root + os.sep)]
    for mp in sorted(targets, key=lambda p: p.count(os.sep), reverse=True):
        for attempt in (1, 2):
            try:
                proc = subprocess.run(["umount", mp], capture_output=True, text=True)
            except OSError:
                break
            if proc.returncode == 0:
                break
            time.sleep(attempt)
        else:
            try:
                subprocess.run(["umount", "-l", mp], capture_output=True, text=True)
            except OSError:
                pass


def _cleanup_all(mounts_path="/proc/mounts"):
    for ws in _workspaces:
        if _failed:
            _release_mounts_under(ws.path, mounts_path)
            if _has_mount_under(ws.path, mounts_path):
                print("WARNING: live mounts remain under retained workspace: %s" % ws.path,
                      file=sys.stderr)
            print("RETAINED FOR INSPECTION: %s" % ws.path, file=sys.stderr)
        else:
            ws.cleanup()


def _handle_signal(signum, frame):
    raise SystemExit(128 + signum)


def init():
    global _registered, _original_excepthook
    if _registered:
        return
    _registered = True
    atexit.register(_cleanup_all)
    _original_excepthook = sys.excepthook
    sys.excepthook = _on_unhandled_exception
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, _handle_signal)


class BuildLock:
    def __init__(self, root):
        self.path = os.path.join(root, LOCK_NAME)
        self._fd = None

    def acquire(self):
        self._fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o644)
        try:
            fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(self._fd)
            self._fd = None
            raise WorkspaceError("another Quail Snap build is already running (lock: %s)"
                                 % self.path)

    def release(self):
        if self._fd is not None:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            finally:
                os.close(self._fd)
                self._fd = None


class Workspace:
    def __init__(self, path):
        self.path = path
        self._cleaned = False

    def contains(self, target):
        root = os.path.realpath(self.path)
        real = os.path.realpath(target)
        return real == root or real.startswith(root + os.sep)

    def remove(self, target):
        if not self.contains(target):
            raise ValueError("refusing to remove path outside workspace: %s" % target)
        if os.path.lexists(target):
            if os.path.isdir(target) and not os.path.islink(target):
                shutil.rmtree(target)
            else:
                os.remove(target)

    def cleanup(self):
        if self._cleaned:
            return
        self._cleaned = True
        if os.path.isdir(self.path):
            shutil.rmtree(self.path, ignore_errors=True)

    def cleanup_strict(self):
        if self._cleaned:
            return
        self._cleaned = True
        if os.path.isdir(self.path) and not os.path.islink(self.path):
            shutil.rmtree(self.path)


def _proc_starttime(pid):
    try:
        with open("/proc/%d/stat" % pid, "r") as fh:
            data = fh.read()
        return data.rsplit(")", 1)[1].split()[19]
    except (OSError, IndexError):
        return None


def _owner_alive(meta_path):
    try:
        with open(meta_path, "r") as fh:
            pid_s, start = fh.read().split()
        pid = int(pid_s)
    except (OSError, ValueError):
        return False
    current = _proc_starttime(pid)
    return current is not None and current == start


def create_workspace(root=None):
    root = root or DEFAULT_ROOT
    os.makedirs(root, exist_ok=True)
    os.chmod(root, 0o755)
    path = tempfile.mkdtemp(prefix="quail-snap-", dir=root)
    with open(os.path.join(path, ".quail-snap-owner"), "w") as fh:
        fh.write("%d %s\n" % (os.getpid(), _proc_starttime(os.getpid()) or "unknown"))
    ws = Workspace(path)
    _workspaces.append(ws)
    return ws


def kickoff_cleanup(root=None, mounts_path="/proc/mounts"):
    root = root or DEFAULT_ROOT
    os.makedirs(root, exist_ok=True)
    os.chmod(root, 0o755)
    lock = BuildLock(root)
    lock.acquire()
    try:
        names = sorted(d for d in os.listdir(root) if d.startswith("quail-snap-"))
        root_real = os.path.realpath(root)
        for name in names:
            path = os.path.join(root, name)
            if os.path.islink(path):
                raise WorkspaceError("symlinked workspace entry, refusing: " + path)
            real = os.path.realpath(path)
            if real == root_real or not real.startswith(root_real + os.sep):
                raise WorkspaceError("workspace entry escapes workspace root: " + name)
            if _has_mount_under(path, mounts_path):
                raise WorkspaceError("live mount under old workspace, refusing: " + name)
        for name in names:
            path = os.path.join(root, name)
            Workspace(path).remove(path)
    except BaseException:
        lock.release()
        raise
    print("KICKOFF: previous workspaces cleared from %s" % root)
    return lock


def finalize(ws, exported, lock=None, mounts_path="/proc/mounts"):
    try:
        if exported is not None and os.path.lexists(exported):
            try:
                ws.cleanup_strict()
            except OSError as exc:
                print("WARNING: workspace cleanup failed for %s: %s" % (ws.path, exc),
                      file=sys.stderr)
        else:
            _release_mounts_under(ws.path, mounts_path)
            if _has_mount_under(ws.path, mounts_path):
                print("WARNING: live mounts remain under retained workspace: %s" % ws.path,
                      file=sys.stderr)
            print("WORKSPACE RETAINED FOR INSPECTION: %s" % ws.path, file=sys.stderr)
    finally:
        if ws in _workspaces:
            _workspaces.remove(ws)
        if lock is not None:
            lock.release()


def find_stale_workspaces(root=None):
    root = root or DEFAULT_ROOT
    if not os.path.isdir(root):
        return []
    active = {os.path.realpath(w.path) for w in _workspaces}
    return sorted(d for d in os.listdir(root) if d.startswith("quail-snap-")
                  and os.path.realpath(os.path.join(root, d)) not in active)


def _tree_size(path):
    total = 0
    for dirpath, _dirnames, filenames in os.walk(path):
        for name in filenames:
            try:
                total += os.lstat(os.path.join(dirpath, name)).st_size
            except OSError:
                pass
    return total


def stale_workspaces(root=None):
    root = root or DEFAULT_ROOT
    out = []
    for name in find_stale_workspaces(root):
        path = os.path.join(root, name)
        try:
            st = os.stat(path)
        except OSError:
            continue
        out.append({"name": name, "path": path,
                    "size_bytes": _tree_size(path), "mtime": st.st_mtime})
    return out


def _has_mount_under(path, mounts_path="/proc/mounts"):
    real = os.path.realpath(path)
    for entry in mounts.read_mounts(mounts_path):
        mp = os.path.realpath(entry["mountpoint"])
        if mp == real or mp.startswith(real + os.sep):
            return True
    return False


def reclaim_stale(root, name, mounts_path="/proc/mounts"):
    if not re.fullmatch(r"quail-snap-[A-Za-z0-9_.\\-]+", name):
        raise ValueError("not a quail-snap workspace name: %r" % name)
    path = os.path.join(root, name)
    if os.path.realpath(path) != os.path.realpath(os.path.join(root, name)):
        raise ValueError("path resolution mismatch: %r" % name)
    root_real = os.path.realpath(root)
    if not (os.path.realpath(path) == root_real
            or os.path.realpath(path).startswith(root_real + os.sep)):
        raise ValueError("path escapes workspace root: %r" % name)
    if _has_mount_under(path, mounts_path):
        raise ValueError("live mount under workspace, refusing: %r" % name)
    meta = os.path.join(path, ".quail-snap-owner")
    if os.path.isfile(meta) and _owner_alive(meta):
        raise ValueError("owner process still alive, refusing: %r" % name)
    if os.path.isdir(path):
        Workspace(path).remove(path)
    return path


def reclaim_stale_all(root, names, mounts_path="/proc/mounts"):
    removed = []
    refused = []
    for name in names:
        try:
            reclaim_stale(root, name, mounts_path=mounts_path)
            removed.append(name)
        except ValueError as exc:
            refused.append("%s (%s)" % (name, exc))
    return removed, refused