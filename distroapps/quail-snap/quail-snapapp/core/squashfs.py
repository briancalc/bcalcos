import os
import shutil
import subprocess

from core import initramfs

STRAY_INITRD_NAME = "quail-snap-initrd.img"


class SquashfsError(Exception):
    pass


def _rootfs(workspace):
    return os.path.join(workspace.path, "staging", "rootfs")


def _iso_live(workspace):
    return os.path.join(workspace.path, "staging", "iso", "live")


def build_squashfs(workspace, kernel_version):
    initramfs.assert_no_mounts_under(workspace)
    rootfs = _rootfs(workspace)
    if not workspace.contains(rootfs):
        raise SquashfsError("rootfs outside workspace: " + rootfs)
    if not os.path.isdir(rootfs):
        raise SquashfsError("staged rootfs missing: " + rootfs)
    iso_live = _iso_live(workspace)
    os.makedirs(iso_live, exist_ok=True)
    squashfs_path = os.path.join(iso_live, "filesystem.squashfs")
    cmd = ["mksquashfs", rootfs, squashfs_path,
           "-comp", "gzip", "-b", "131072", "-noappend", "-e", STRAY_INITRD_NAME]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise SquashfsError("mksquashfs failed (exit %d): %s"
                            % (proc.returncode, proc.stderr.strip()))
    if not os.path.isfile(squashfs_path):
        raise SquashfsError("mksquashfs produced no output at " + squashfs_path)
    kernel_path = os.path.join(rootfs, "boot", "vmlinuz-" + kernel_version)
    if not os.path.isfile(kernel_path):
        raise SquashfsError("staged kernel for version %s missing: %s"
                            % (kernel_version, kernel_path))
    shutil.copy2(kernel_path, os.path.join(iso_live, "vmlinuz"))
    initrd_src = os.path.join(workspace.path, "staging", "initrd.img")
    if not os.path.isfile(initrd_src):
        raise SquashfsError("staged initrd missing: " + initrd_src)
    shutil.copy2(initrd_src, os.path.join(iso_live, "initrd.img"))
    return {"squashfs": squashfs_path, "kernel": os.path.join(iso_live, "vmlinuz"),
            "initrd": os.path.join(iso_live, "initrd.img"), "kernel_version": kernel_version}