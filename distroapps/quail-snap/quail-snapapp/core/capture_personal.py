import os
import subprocess

from core import workspace as ws

RSYNC_FLAGS = ("-aAXH", "--numeric-ids", "--one-file-system")

RSYNC_VANISHED_EXIT = 24

ROOT_EXCLUDED_PATHS = (
    "/proc/*",
    "/sys/*",
    "/dev/*",
    "/run/*",
    "/tmp/*",
    "/var/tmp/*",
    "/var/crash/*",
    "/lost+found",
)

HOME_SOURCE = "/home"


class PersonalCaptureError(Exception):
    def __init__(self, returncode, message):
        super().__init__("personal capture failed (exit %s): %s"
                         % (returncode, message.strip()))
        self.returncode = returncode
        self.stderr = message


def _rootfs(workspace):
    return os.path.join(workspace.path, "staging", "rootfs")


def build_rsync_command(source, dest, excludes=(), dry_run=False):
    cmd = ["rsync"] + list(RSYNC_FLAGS)
    if dry_run:
        cmd.append("--dry-run")
    for pattern in excludes:
        cmd.append("--exclude=" + pattern)
    cmd.append(source.rstrip("/") + "/")
    cmd.append(dest.rstrip("/") + "/")
    return cmd


def _home_relative_excludes(paths, home):
    home_abs = os.path.abspath(home).rstrip("/") or "/"
    patterns = []
    for p in paths:
        ap = os.path.abspath(p).rstrip("/")
        rel = os.path.relpath(ap, home_abs)
        if rel == ".":
            raise PersonalCaptureError(-1, "configured path equals the home capture "
                                           "source: " + ap)
        if rel == ".." or rel.startswith(".." + os.sep):
            continue
        patterns.append("/" + rel)
    return patterns


def _run(cmd):
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode == RSYNC_VANISHED_EXIT:
        raise PersonalCaptureError(proc.returncode,
                                   "source files vanished during capture; "
                                   "snapshot may be incomplete: " + proc.stderr)
    if proc.returncode != 0:
        raise PersonalCaptureError(proc.returncode, proc.stderr)
    return proc


def _workspace_root(workspace_root):
    if workspace_root is not None:
        return os.path.abspath(workspace_root)
    return ws.DEFAULT_ROOT


def capture_root(workspace, export_root, workspace_root=None, source="/", dry_run=False):
    dest = _rootfs(workspace)
    if not workspace.contains(dest):
        raise PersonalCaptureError(-1, "destination outside workspace: " + dest)
    os.makedirs(dest, exist_ok=True)
    wroot = _workspace_root(workspace_root)
    excludes = list(ROOT_EXCLUDED_PATHS)
    for p in (wroot, export_root):
        excludes.append(os.path.abspath(p).rstrip("/") + "/*")
    cmd = build_rsync_command(source, dest, excludes, dry_run=dry_run)
    return _run(cmd)


def capture_home(workspace, export_root, workspace_root=None, source=HOME_SOURCE,
                 creator=None, dry_run=False):
    rootfs = _rootfs(workspace)
    if creator is not None:
        if not creator or "\x00" in creator or "/" in creator \
                or creator in (".", ".."):
            raise PersonalCaptureError(-1, "unsafe creator name for home capture: %r"
                                       % creator)
        src = os.path.join(source, creator)
        src_real = os.path.realpath(src)
        source_real = os.path.realpath(source)
        if src_real != source_real and not src_real.startswith(source_real + os.sep):
            raise PersonalCaptureError(-1, "creator home escapes the home source: "
                                       + src)
        if os.path.islink(src) or not os.path.isdir(src_real):
            raise PersonalCaptureError(-1, "creator home is missing, a symlink, or not "
                                       "a directory: " + src)
        dest = os.path.join(rootfs, "home", creator)
    else:
        src = source
        dest = os.path.join(rootfs, "home")
    if not workspace.contains(dest):
        raise PersonalCaptureError(-1, "destination outside workspace: " + dest)
    os.makedirs(dest, exist_ok=True)
    wroot = _workspace_root(workspace_root)
    excludes = _home_relative_excludes((wroot, export_root), src)
    cmd = build_rsync_command(src, dest, excludes, dry_run=dry_run)
    return _run(cmd)


def capture(workspace, export_root, workspace_root=None, source="/",
            home_source=HOME_SOURCE, creator=None, dry_run=False):
    root_proc = capture_root(workspace, export_root, workspace_root=workspace_root,
                             source=source, dry_run=dry_run)
    home_proc = capture_home(workspace, export_root, workspace_root=workspace_root,
                             source=home_source, creator=creator, dry_run=dry_run)
    return root_proc, home_proc