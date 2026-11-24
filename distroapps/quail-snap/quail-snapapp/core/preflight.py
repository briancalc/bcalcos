import os
import shutil
import subprocess
from dataclasses import dataclass, field

from core import workspace as ws

OK = "ok"
WARN = "warn"
FAIL = "fail"

# sha256sum is required: it is the sole verification tool for a completed ISO.
# dpkg-query is required: the boot tree stage (bootloader.write_pkglist) runs it
# and the ISO verification requires its output. chroot is deliberately not listed:
# it is coreutils-present on any viable host.
REQUIRED_BINARIES = ("rsync", "mkpasswd", "sha256sum", "dpkg-query")
STAGED_BINARIES = ("mksquashfs", "xorriso", "mkdosfs", "grub-mkimage", "mkinitramfs",
                   "mmd", "mcopy", "lsinitramfs", "mount", "umount")
ISOLINUX_FILE_CANDIDATES = {
    "isolinux.bin": ("/usr/lib/ISOLINUX/isolinux.bin", "/usr/lib/syslinux/bios/isolinux.bin"),
    "ldlinux.c32": ("/usr/lib/syslinux/modules/bios/ldlinux.c32",),
}
BCALCOS_PACKAGE = "bcalcos-base"
# Packages the live-ISO build requires on the build host. Each must be in
# dpkg status exactly "install ok installed". Backend packages are listed
# explicitly: an installed parent (live-boot / live-config) does not prove
# its backend (live-boot-initramfs-tools / live-config-sysvinit) is installed
# and configured, so every entry is checked independently.
REQUIRED_LIVE_PACKAGES = (
    "live-boot",
    "live-boot-initramfs-tools",
    "live-config",
    "live-config-sysvinit",
    "memtest86+",
)
LIVE_PACKAGES_INSTALL_CMD = "sudo apt install " + " ".join(REQUIRED_LIVE_PACKAGES)
SPACE_FLOOR_BYTES = 2 * 1024 ** 3


@dataclass
class CheckResult:
    name: str
    status: str
    detail: str


@dataclass
class PreflightReport:
    results: list = field(default_factory=list)

    def add(self, result):
        self.results.append(result)

    @property
    def ok(self):
        return all(r.status != FAIL for r in self.results)


def _fmt_size(n):
    value = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            if unit == "B":
                return "%d B" % n
            return "%.1f %s" % (value, unit)
        value /= 1024


def check_root():
    if os.geteuid() == 0:
        return CheckResult("privileges", OK, "running as root")
    return CheckResult("privileges", FAIL, "must be run as root (euid 0)")


def _check_package(pkg, missing_status=WARN):
    # Fail closed: for FAIL-grade checks (bcalcos-base, the required live
    # packages) an inability to verify must produce FAIL, never WARN.
    if shutil.which("dpkg-query") is None:
        return CheckResult("package: %s" % pkg, missing_status,
                           "dpkg-query unavailable; cannot verify")
    try:
        proc = subprocess.run(
            ["dpkg-query", "-W", "-f=${Status}", pkg],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return CheckResult("package: %s" % pkg, missing_status,
                           "check failed: %s" % exc)
    status = proc.stdout.strip()
    if proc.returncode == 0 and status == "install ok installed":
        return CheckResult("package: %s" % pkg, OK, "installed")
    if proc.returncode == 0:
        return CheckResult("package: %s" % pkg, missing_status,
                           "not fully configured (dpkg status: %s)" % (status or "unknown"))
    return CheckResult("package: %s" % pkg, missing_status, "not installed")


def check_dependencies():
    results = [_check_package(BCALCOS_PACKAGE, missing_status=FAIL)]
    missing = [b for b in REQUIRED_BINARIES if shutil.which(b) is None]
    if missing:
        results.append(CheckResult("required tools", FAIL, "missing: " + ", ".join(missing)))
    else:
        results.append(CheckResult("required tools", OK, "present: " + ", ".join(REQUIRED_BINARIES)))
    missing = [b for b in STAGED_BINARIES if shutil.which(b) is None]
    if missing:
        results.append(CheckResult("tools for later stages", WARN, "not installed: " + ", ".join(missing)))
    else:
        results.append(CheckResult("tools for later stages", OK, "present: " + ", ".join(STAGED_BINARIES)))
    for label, candidates in ISOLINUX_FILE_CANDIDATES.items():
        found = next((p for p in candidates if os.path.isfile(p)), None)
        if found:
            results.append(CheckResult("isolinux file: %s" % label, OK, found))
        else:
            results.append(CheckResult("isolinux file: %s" % label, WARN,
                                       "not found in: " + ", ".join(candidates)))
    missing_live = []
    for pkg in REQUIRED_LIVE_PACKAGES:
        result = _check_package(pkg, missing_status=FAIL)
        results.append(result)
        if result.status == FAIL:
            missing_live.append(pkg)
    if missing_live:
        results.append(CheckResult(
            "required live packages", FAIL,
            "\nISO builds require:\n" + "\n".join(REQUIRED_LIVE_PACKAGES)
            + "\n\nInstall them:\n" + LIVE_PACKAGES_INSTALL_CMD
            + "\n\nMissing or not fully configured: " + ", ".join(missing_live)))
    return results


def check_source_state():
    try:
        with open("/proc/cmdline", "r") as fh:
            cmdline = fh.read()
    except OSError as exc:
        return CheckResult("source state", WARN, "cannot read /proc/cmdline: %s" % exc)
    if "boot=live" in cmdline.split():
        return CheckResult("source state", FAIL,
                           "running from a live session; capture must run on the installed system")
    return CheckResult("source state", OK, "running on the installed system")


def check_disk_space(workspace_root):
    try:
        root_stat = os.statvfs("/")
        used = (root_stat.f_blocks - root_stat.f_bfree) * root_stat.f_frsize
        required = used + used // 2 + SPACE_FLOOR_BYTES
        ws_stat = os.statvfs(workspace_root)
        available = ws_stat.f_bavail * ws_stat.f_frsize
    except OSError as exc:
        return CheckResult("disk space", WARN, "cannot stat filesystem: %s" % exc)
    if available < required:
        return CheckResult("disk space", FAIL,
                           "need ~%s on %s, have %s" % (_fmt_size(required), workspace_root, _fmt_size(available)))
    return CheckResult("disk space", OK,
                       "need ~%s (heuristic), have %s on %s" % (_fmt_size(required), _fmt_size(available), workspace_root))


def _probe_writable(workspace_root):
    probe = os.path.join(workspace_root, ".probe")
    try:
        os.makedirs(probe)
        os.rmdir(probe)
    except OSError as exc:
        return CheckResult("workspace root", FAIL, "not writable: %s: %s" % (workspace_root, exc))
    return CheckResult("workspace root", OK, "writable: " + workspace_root)


def _stale_workspaces(workspace_root):
    stale = ws.find_stale_workspaces(workspace_root)
    if stale:
        return CheckResult("stale workspaces", WARN,
                           "%d leftover workspace(s) under %s" % (len(stale), workspace_root))
    return CheckResult("stale workspaces", OK, "none under " + workspace_root)


def run_preflight(workspace_root=ws.DEFAULT_ROOT):
    report = PreflightReport()
    report.add(check_root())
    for result in check_dependencies():
        report.add(result)
    report.add(check_source_state())
    try:
        os.makedirs(workspace_root, exist_ok=True)
    except OSError as exc:
        report.add(CheckResult("workspace root", FAIL, "cannot create %s: %s" % (workspace_root, exc)))
        return report
    report.add(_probe_writable(workspace_root))
    report.add(_stale_workspaces(workspace_root))
    report.add(check_disk_space(workspace_root))
    return report