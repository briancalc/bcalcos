import os
import shutil
import subprocess

from core import mounts
from core import preflight
from core import workspace as ws

OK = preflight.OK
WARN = preflight.WARN
FAIL = preflight.FAIL
CheckResult = preflight.CheckResult
PreflightReport = preflight.PreflightReport

REQUIRED_BINARIES = ("rsync", "sha256sum", "dpkg-query", "mkpasswd")
STAGED_BINARIES = ("mksquashfs", "xorriso", "mkdosfs", "grub-mkimage", "mkinitramfs",
                   "mmd", "mcopy", "lsinitramfs", "mount", "umount")
LIVE_PACKAGES = ("live-boot", "live-config")
BCALCOS_PACKAGE = "bcalcos-base"
ISOLINUX_FILE_CANDIDATES = {
    "isolinux.bin": ("/usr/lib/ISOLINUX/isolinux.bin", "/usr/lib/syslinux/bios/isolinux.bin"),
    "ldlinux.c32": ("/usr/lib/syslinux/modules/bios/ldlinux.c32",),
}
HOME_SOURCE = "/home"
SPACE_FACTOR = 2
SPACE_FLOOR_BYTES = 2 * 1024 ** 3
GPG_CIPHER_TOKEN = "AES256"


def _fmt_size(n):
    value = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            if unit == "B":
                return "%d B" % n
            return "%.1f %s" % (value, unit)
        value /= 1024


def check_gpg():
    if shutil.which("gpg") is None:
        return CheckResult("gnupg", FAIL, "gpg not found in PATH")
    try:
        proc = subprocess.run(["gpg", "--version"], capture_output=True, text=True,
                              timeout=15)
    except (OSError, subprocess.SubprocessError) as exc:
        return CheckResult("gnupg", FAIL, "gpg --version failed: %s" % exc)
    if proc.returncode != 0:
        return CheckResult("gnupg", FAIL,
                           "gpg --version exited %d: %s"
                           % (proc.returncode, proc.stderr.strip()))
    if GPG_CIPHER_TOKEN not in proc.stdout:
        return CheckResult("gnupg", FAIL,
                           "AES256 not listed in gpg --version cipher algorithms")
    version = proc.stdout.splitlines()[0] if proc.stdout.splitlines() else "gpg"
    return CheckResult("gnupg", OK, "%s, AES256 supported" % version)


def _check_package(pkg, missing_status=WARN):
    if shutil.which("dpkg-query") is None:
        return CheckResult("package: %s" % pkg, WARN, "dpkg-query unavailable; cannot verify")
    try:
        proc = subprocess.run(["dpkg-query", "-W", "-f=${Status}", pkg],
                              capture_output=True, text=True, timeout=10)
        installed = proc.returncode == 0 and "install ok installed" in proc.stdout
    except (OSError, subprocess.SubprocessError) as exc:
        return CheckResult("package: %s" % pkg, WARN, "check failed: %s" % exc)
    if installed:
        return CheckResult("package: %s" % pkg, OK, "installed")
    return CheckResult("package: %s" % pkg, missing_status, "not installed")


def check_dependencies():
    results = [_check_package(BCALCOS_PACKAGE, missing_status=FAIL)]
    missing = [b for b in REQUIRED_BINARIES if shutil.which(b) is None]
    if missing:
        results.append(CheckResult("required tools", FAIL, "missing: " + ", ".join(missing)))
    else:
        results.append(CheckResult("required tools", OK,
                                   "present: " + ", ".join(REQUIRED_BINARIES)))
    missing = [b for b in STAGED_BINARIES if shutil.which(b) is None]
    if missing:
        results.append(CheckResult("tools for later stages", WARN,
                                   "not installed: " + ", ".join(missing)))
    else:
        results.append(CheckResult("tools for later stages", OK,
                                   "present: " + ", ".join(STAGED_BINARIES)))
    for label, candidates in ISOLINUX_FILE_CANDIDATES.items():
        found = next((p for p in candidates if os.path.isfile(p)), None)
        if found:
            results.append(CheckResult("isolinux file: %s" % label, OK, found))
        else:
            results.append(CheckResult("isolinux file: %s" % label, WARN,
                                       "not found in: " + ", ".join(candidates)))
    for pkg in LIVE_PACKAGES:
        results.append(_check_package(pkg))
    return results


def check_home_filesystem(mounts_path="/proc/mounts"):
    try:
        entries = mounts.read_mounts(mounts_path)
    except OSError as exc:
        return CheckResult("home filesystem", FAIL,
                           "cannot read %s: %s" % (mounts_path, exc))
    root = next((e for e in entries if e["mountpoint"] == "/"), None)
    home = next((e for e in entries if e["mountpoint"] == HOME_SOURCE), None)
    if root is None:
        return CheckResult("home filesystem", FAIL,
                           "cannot find the root mount entry in %s" % mounts_path)
    if home is None:
        return CheckResult("home filesystem", FAIL,
                           "%s is not mounted; the Personal build requires the separate "
                           "/home partition" % HOME_SOURCE)
    try:
        root_dev = os.stat("/").st_dev
        home_dev = os.stat(HOME_SOURCE).st_dev
    except OSError as exc:
        return CheckResult("home filesystem", FAIL,
                           "cannot stat / or %s to determine filesystem devices: %s"
                           % (HOME_SOURCE, exc))
    if home_dev == root_dev:
        return CheckResult("home filesystem", FAIL,
                           "%s is on the same filesystem as / (device %d); the separate "
                           "/home partition is required" % (HOME_SOURCE, home_dev))
    return CheckResult("home filesystem", OK,
                       "%s mounted (%s) on device %d, separate from / (device %d)"
                       % (HOME_SOURCE, home["fstype"], home_dev, root_dev))


def _workspace_root_check(workspace_root):
    if not os.path.isdir(workspace_root):
        return CheckResult("workspace root", WARN,
                           "not present yet (created at workspace kickoff): "
                           + workspace_root)
    if not os.access(workspace_root, os.W_OK | os.X_OK):
        return CheckResult("workspace root", FAIL, "not writable: " + workspace_root)
    return CheckResult("workspace root", OK, "writable: " + workspace_root)


def _stale_workspaces(workspace_root):
    stale = ws.find_stale_workspaces(workspace_root)
    if stale:
        return CheckResult("stale workspaces", WARN,
                           "%d leftover workspace(s) under %s" % (len(stale), workspace_root))
    return CheckResult("stale workspaces", OK, "none under " + workspace_root)


def _used_bytes(stat):
    return (stat.f_blocks - stat.f_bfree) * stat.f_frsize


def _nearest_existing_dir(path):
    cur = os.path.abspath(path)
    while not os.path.isdir(cur):
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    return cur


def _tree_size_strict(path):
    def onerr(err):
        raise err
    total = 0
    for dirpath, _dirnames, filenames in os.walk(path, followlinks=False, onerror=onerr):
        for name in filenames:
            full = os.path.join(dirpath, name)
            total += os.lstat(full).st_size
    return total


def check_disk_space_initial(workspace_root=None):
    workspace_root = workspace_root or ws.DEFAULT_ROOT
    try:
        root_stat = os.statvfs("/")
        used_root = _used_bytes(root_stat)
        required = SPACE_FACTOR * used_root + SPACE_FLOOR_BYTES
        ws_dir = workspace_root if os.path.isdir(workspace_root) \
            else _nearest_existing_dir(workspace_root)
        ws_stat = os.statvfs(ws_dir)
        available = ws_stat.f_bavail * ws_stat.f_frsize
    except OSError as exc:
        return CheckResult("disk space (initial)", FAIL, "cannot measure: %s" % exc)
    detail = ("planning estimate (not a guaranteed bound): need ~%s "
              "[%d x used(/) + %s; only the creator's /home is captured, and the "
              "post-capture check re-measures the staged tree] on %s, available %s"
              % (_fmt_size(required), SPACE_FACTOR, _fmt_size(SPACE_FLOOR_BYTES),
                 ws_dir, _fmt_size(available)))
    if available < required:
        return CheckResult("disk space (initial)", FAIL, detail)
    return CheckResult("disk space (initial)", OK, detail)


def check_space_after_capture(staged_rootfs, workspace_root=None):
    workspace_root = workspace_root or ws.DEFAULT_ROOT
    try:
        measured = _tree_size_strict(staged_rootfs)
        ws_dir = workspace_root if os.path.isdir(workspace_root) \
            else _nearest_existing_dir(workspace_root)
        ws_stat = os.statvfs(ws_dir)
        available = ws_stat.f_bavail * ws_stat.f_frsize
    except OSError as exc:
        return CheckResult("disk space (post-capture)", FAIL, "cannot measure: %s" % exc)
    required = SPACE_FACTOR * measured + SPACE_FLOOR_BYTES
    detail = ("staged tree %s; planning estimate (not a guaranteed bound): need ~%s "
              "on %s, available %s"
              % (_fmt_size(measured), _fmt_size(required), ws_dir,
                 _fmt_size(available)))
    if available < required:
        return CheckResult("disk space (post-capture)", FAIL, detail)
    return CheckResult("disk space (post-capture)", OK, detail)


def run_preflight(workspace_root=None):
    workspace_root = workspace_root or ws.DEFAULT_ROOT
    report = PreflightReport()
    report.add(preflight.check_root())
    report.add(preflight.check_source_state())
    report.add(check_home_filesystem())
    report.add(check_gpg())
    for result in check_dependencies():
        report.add(result)
    report.add(_workspace_root_check(workspace_root))
    report.add(_stale_workspaces(workspace_root))
    report.add(check_disk_space_initial(workspace_root))
    return report