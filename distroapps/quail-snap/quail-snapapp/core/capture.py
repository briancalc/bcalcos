import os
import subprocess

RSYNC_FLAGS = ("-aAXH", "--numeric-ids", "--one-file-system")

RSYNC_VANISHED_EXIT = 24

EXCLUDED_PATHS = (
    "/proc/*",
    "/sys/*",
    "/dev/*",
    "/run/*",
    "/tmp/*",
    "/var/tmp/*",
    "/var/crash/*",
    "/var/lib/libvirt/*",
    "/lost+found",
    "/var/spool/cron/*",
    "/var/mail/*",
    "/var/spool/mail/*",
)


class CaptureError(Exception):
    def __init__(self, returncode, message):
        super().__init__("capture failed (exit %s): %s" % (returncode, message.strip()))
        self.returncode = returncode
        self.stderr = message


def build_rsync_command(source, dest, workspace_root, export_root, dry_run=False):
    cmd = ["rsync"] + list(RSYNC_FLAGS)
    if dry_run:
        cmd.append("--dry-run")
    for pattern in EXCLUDED_PATHS:
        cmd.append("--exclude=" + pattern)
    cmd.append("--exclude=" + workspace_root.rstrip("/") + "/*")
    cmd.append("--exclude=" + export_root.rstrip("/") + "/*")
    cmd.append(source.rstrip("/") + "/")
    cmd.append(dest.rstrip("/") + "/")
    return cmd


def capture(workspace, export_root, source="/", dry_run=False):
    dest = os.path.join(workspace.path, "staging", "rootfs")
    if not workspace.contains(dest):
        raise CaptureError(-1, "destination outside workspace: " + dest)
    os.makedirs(dest, exist_ok=True)
    cmd = build_rsync_command(source, dest, workspace.path, export_root, dry_run=dry_run)
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode == RSYNC_VANISHED_EXIT:
        raise CaptureError(proc.returncode,
                           "source files vanished during capture; "
                           "snapshot may be incomplete: " + proc.stderr)
    if proc.returncode != 0:
        raise CaptureError(proc.returncode, proc.stderr)
    return proc