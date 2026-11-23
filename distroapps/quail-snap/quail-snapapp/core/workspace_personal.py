import atexit
import os
import signal
import stat
import subprocess
import sys
import threading
import time

from core import mounts
from core import workspace

_personal_workspaces = []
_registered = False
_signals_registered = False
_pending_signal = None


class PersonalWorkspaceError(Exception):
    pass


def has_mount_under(path, mounts_path="/proc/mounts"):
    root = os.path.realpath(path)
    for entry in mounts.read_mounts(mounts_path):
        mp = os.path.realpath(entry["mountpoint"])
        if mp == root or mp.startswith(root + os.sep):
            return True
    return False


def _umount(mountpoint, lazy=False):
    cmd = ["umount"] + (["-l"] if lazy else []) + [mountpoint]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True)
    except OSError:
        return 1
    return proc.returncode


def _release_mounts_under(path, mounts_path="/proc/mounts"):
    root = os.path.realpath(path)
    targets = [os.path.realpath(e["mountpoint"]) for e in mounts.read_mounts(mounts_path)]
    targets = [mp for mp in targets if mp == root or mp.startswith(root + os.sep)]
    for mp in sorted(targets, key=lambda p: p.count(os.sep), reverse=True):
        for attempt in (1, 2):
            if _umount(mp) == 0:
                break
            time.sleep(attempt)
        else:
            _umount(mp, lazy=True)


def _retain(ws, reason):
    if os.path.isdir(ws.path) and not os.path.islink(ws.path):
        try:
            os.chmod(ws.path, 0o700)
        except OSError:
            pass
    print("SECURE CLEANUP UNSUCCESSFUL: %s: %s" % (reason, ws.path), file=sys.stderr)


def _retain_for_inspection(ws, reason):
    if os.path.islink(ws.path) or not os.path.isdir(ws.path):
        print("WARNING: retained workspace path %s is missing, a symlink, or not a "
              "directory; mode 0700 NOT confirmed" % ws.path, file=sys.stderr)
        print("WORKSPACE RETAINED (0700 NOT confirmed): %s: %s" % (reason, ws.path),
              file=sys.stderr)
        return False
    try:
        os.chmod(ws.path, 0o700)
        mode = stat.S_IMODE(os.lstat(ws.path).st_mode)
        if mode != 0o700:
            raise OSError("mode is %04o after chmod" % mode)
    except OSError as exc:
        print("WARNING: retained workspace %s is not confirmed at mode 0700 (%s); "
              "its contents may be readable by others" % (ws.path, exc),
              file=sys.stderr)
        print("WORKSPACE RETAINED (0700 NOT confirmed): %s: %s" % (reason, ws.path),
              file=sys.stderr)
        return False
    print("WORKSPACE RETAINED FOR INSPECTION (0700): %s: %s" % (reason, ws.path),
          file=sys.stderr)
    return True


def _wipe_or_retain(ws, mounts_path="/proc/mounts", retain=False):
    root = os.path.realpath(ws.path)
    _release_mounts_under(root, mounts_path)
    if has_mount_under(root, mounts_path):
        _retain(ws, "live mount(s) remain under retained workspace")
        return False
    if retain:
        return _retain_for_inspection(ws, "failed Personal build")
    try:
        ws.cleanup_strict()
    except OSError as exc:
        _retain(ws, "workspace deletion failed: %s" % exc)
        return False
    if os.path.lexists(ws.path):
        _retain(ws, "workspace still present after cleanup")
        return False
    return True


def _cleanup_all_personal(mounts_path="/proc/mounts"):
    for ws in list(_personal_workspaces):
        try:
            _release_mounts_under(ws.path, mounts_path)
            _retain_for_inspection(ws, "application shutdown before build finalization")
        except OSError as exc:
            print("WARNING: personal workspace cleanup failed for %s: %s"
                  % (ws.path, exc), file=sys.stderr)


def _handle_signal(signum, frame):
    global _pending_signal
    if _pending_signal is None:
        _pending_signal = signum


def pending_signal():
    return _pending_signal


def init_personal():
    global _registered, _signals_registered
    if not _registered:
        _registered = True
        atexit.register(_cleanup_all_personal)
    if threading.current_thread() is threading.main_thread():
        if not _signals_registered:
            _signals_registered = True
            for sig in (signal.SIGINT, signal.SIGTERM):
                signal.signal(sig, _handle_signal)
    elif not _signals_registered:
        raise PersonalWorkspaceError(
            "signal handlers can only be registered from the main thread; "
            "call init_personal() on the main thread first")


def create_personal_workspace(root=None):
    lock = workspace.kickoff_cleanup(root=root)
    try:
        ws = workspace.create_workspace(root=root)
    except BaseException:
        lock.release()
        raise
    _personal_workspaces.append(ws)
    return ws, lock


def finalize_personal(ws, lock=None, mounts_path="/proc/mounts", retain=False):
    try:
        return _wipe_or_retain(ws, mounts_path, retain=retain)
    finally:
        if ws in _personal_workspaces:
            _personal_workspaces.remove(ws)
        if lock is not None:
            lock.release()