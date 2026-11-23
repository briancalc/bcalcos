import contextlib
import hashlib
import os
import shutil
import subprocess
import tempfile
import time

from core import mounts

REQUIRED_INITRD_PATHS = ("scripts/live", "usr/bin/live-boot")
LIVE_BOOT_SUPPORT_PREFIX = "usr/lib/live/boot/"


class InitramfsError(Exception):
    pass


def _rootfs(workspace):
    return os.path.join(workspace.path, "staging", "rootfs")


def select_kernel(rootfs, release=None, workspace=None):
    if release is None:
        release = os.uname().release
    if workspace is not None and not workspace.contains(rootfs):
        raise InitramfsError("staged rootfs outside workspace: " + rootfs)
    root_real = os.path.realpath(rootfs)
    boot = os.path.join(rootfs, "boot")
    if not os.path.isdir(boot):
        raise InitramfsError("staged /boot missing: " + boot)
    kernel_path = os.path.join(boot, "vmlinuz-" + release)
    if os.path.islink(kernel_path) or not os.path.isfile(kernel_path):
        raise InitramfsError("running kernel image missing from staged /boot: "
                             + kernel_path)
    modules_dir = os.path.join(rootfs, "lib", "modules", release)
    if os.path.islink(modules_dir) or not os.path.isdir(modules_dir):
        raise InitramfsError("running kernel modules missing from staged /lib/modules: "
                             + modules_dir)
    for p in (kernel_path, modules_dir):
        real = os.path.realpath(p)
        if real == root_real or not real.startswith(root_real + os.sep):
            raise InitramfsError("kernel path escapes staged rootfs: " + p)
    return kernel_path, release


def _umount_checked(dest):
    for attempt in (1, 2):
        proc = subprocess.run(["umount", dest], capture_output=True, text=True)
        if proc.returncode == 0:
            return
        time.sleep(attempt)
    proc = subprocess.run(["umount", "-l", dest], capture_output=True, text=True)
    if proc.returncode != 0:
        raise InitramfsError("could not unmount %s: %s" % (dest, proc.stderr.strip()))


@contextlib.contextmanager
def _chroot_mounts(rootfs):
    mounted = []
    try:
        for rel in ("proc", "dev", "sys"):
            dest = os.path.join(rootfs, rel)
            os.makedirs(dest, exist_ok=True)
            subprocess.run(["mount", "--bind", "/" + rel, dest],
                           check=True, capture_output=True, text=True)
            mounted.append(dest)
        yield
    finally:
        for dest in reversed(mounted):
            _umount_checked(dest)


def assert_no_mounts_under(workspace, mounts_path="/proc/mounts"):
    root = os.path.realpath(workspace.path)
    for entry in mounts.read_mounts(mounts_path):
        mp = os.path.realpath(entry["mountpoint"])
        if mp == root or mp.startswith(root + os.sep):
            raise InitramfsError("mountpoint inside workspace: " + entry["mountpoint"])


def _kernel_prune_plan(rootfs, workspace, release):
    plan = []
    boot = os.path.join(rootfs, "boot")
    if os.path.isdir(boot):
        for name in os.listdir(boot):
            if name.startswith("vmlinuz-") and name != "vmlinuz-" + release:
                plan.append((os.path.join(boot, name), "file"))
            elif name.startswith("initrd.img-") and name != "initrd.img-" + release:
                plan.append((os.path.join(boot, name), "file"))
    modules_root = os.path.join(rootfs, "lib", "modules")
    if os.path.isdir(modules_root):
        for name in os.listdir(modules_root):
            if name != release:
                plan.append((os.path.join(modules_root, name), "dir"))
    root_real = os.path.realpath(rootfs)
    validated = []
    for target, kind in plan:
        if os.path.islink(target):
            raise InitramfsError("symlinked kernel artifact, refusing: " + target)
        if kind == "file" and not os.path.isfile(target):
            raise InitramfsError("kernel artifact is not a regular file: " + target)
        if kind == "dir" and not os.path.isdir(target):
            raise InitramfsError("kernel module entry is not a directory: " + target)
        real = os.path.realpath(target)
        if not real.startswith(root_real + os.sep) or not workspace.contains(target):
            raise InitramfsError("kernel artifact escapes staged rootfs: " + target)
        validated.append(target)
    return validated


def prune_other_kernels(rootfs, workspace, release):
    for target in _kernel_prune_plan(rootfs, workspace, release):
        if os.path.lexists(target):
            workspace.remove(target)


def _sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _install_initrd_into_boot(rootfs, workspace, version, initrd_path):
    if not version or os.sep in version or version in (".", ".."):
        raise InitramfsError("invalid kernel release: %r" % version)
    if os.path.islink(rootfs) or not os.path.isdir(rootfs):
        raise InitramfsError("staged rootfs missing or not a directory: " + rootfs)
    boot = os.path.join(rootfs, "boot")
    if os.path.islink(boot) or not os.path.isdir(boot):
        raise InitramfsError("staged /boot missing or not a directory: " + boot)
    target = os.path.join(boot, "initrd.img-" + version)
    if os.path.islink(target):
        raise InitramfsError("symlinked initrd target, refusing: " + target)
    if os.path.lexists(target) and not os.path.isfile(target):
        raise InitramfsError("staged /boot/initrd.img-%s is not a regular file: %s"
                             % (version, target))
    root_real = os.path.realpath(rootfs)
    target_real = os.path.realpath(target)
    if target_real == root_real or not target_real.startswith(root_real + os.sep):
        raise InitramfsError("initrd target escapes staged rootfs: " + target)
    if not workspace.contains(target):
        raise InitramfsError("initrd target outside workspace: " + target)
    if not os.path.isfile(initrd_path):
        raise InitramfsError("generated initramfs missing: " + initrd_path)
    fd, tmp = tempfile.mkstemp(prefix="initrd.img-", suffix=".quail-snap-tmp",
                               dir=boot)
    os.close(fd)
    try:
        shutil.copyfile(initrd_path, tmp)
        os.chmod(tmp, os.stat(initrd_path).st_mode & 0o7777)
        if _sha256_file(tmp) != _sha256_file(initrd_path):
            raise InitramfsError("staged initrd copy digest does not match "
                                 "the generated image")
        os.replace(tmp, target)
    except BaseException:
        if os.path.lexists(tmp) and not os.path.islink(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
        raise
    return target


def build_initramfs(workspace, release=None):
    rootfs = _rootfs(workspace)
    kernel_path, version = select_kernel(rootfs, release=release, workspace=workspace)
    if not workspace.contains(rootfs):
        raise InitramfsError("rootfs outside workspace: " + rootfs)
    out_inside = os.path.join(rootfs, "quail-snap-initrd.img")
    if os.path.lexists(out_inside):
        workspace.remove(out_inside)
    with _chroot_mounts(rootfs):
        proc = subprocess.run(
            ["chroot", rootfs, "mkinitramfs", "-o", "/quail-snap-initrd.img", version],
            capture_output=True, text=True)
    if proc.returncode != 0:
        raise InitramfsError("mkinitramfs failed (exit %d): %s" % (proc.returncode, proc.stderr.strip()))
    if not os.path.isfile(out_inside):
        raise InitramfsError("mkinitramfs produced no output at " + out_inside)
    dest = os.path.join(workspace.path, "staging", "initrd.img")
    os.replace(out_inside, dest)
    _install_initrd_into_boot(rootfs, workspace, version, dest)
    prune_other_kernels(rootfs, workspace, version)
    return dest, version


def _norm(entry):
    return entry.lstrip("./")


def _norm_layout(entry):
    entry = _norm(entry)
    if entry.startswith("usr/"):
        return entry[len("usr/"):]
    return entry


def verify_initramfs(path, kernel_version):
    proc = subprocess.run(["lsinitramfs", path], capture_output=True, text=True)
    if proc.returncode != 0:
        raise InitramfsError("lsinitramfs failed (exit %d): %s" % (proc.returncode, proc.stderr.strip()))
    entries = [_norm(e) for e in proc.stdout.splitlines() if e.strip()]
    layout_entries = [_norm_layout(e) for e in entries]
    missing = [p for p in REQUIRED_INITRD_PATHS if p not in entries]
    if missing:
        raise InitramfsError("initramfs missing live-boot paths: " + ", ".join(missing))
    support = [e for e in entries if e.startswith(LIVE_BOOT_SUPPORT_PREFIX)]
    if not support:
        raise InitramfsError("initramfs has no usr/lib/live/boot/ support files "
                             "(reference ISO's initrd had 22)")
    modules_dir = "lib/modules/" + kernel_version
    if not any(e == modules_dir or e.startswith(modules_dir + "/") for e in layout_entries):
        raise InitramfsError("initramfs does not carry modules for kernel " + kernel_version)
    return {"live_boot_support_files": len(support), "kernel_version": kernel_version}