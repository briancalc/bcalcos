import os
import shutil
import subprocess
import tempfile
import time

LIVEBOOT_PAYLOAD_REL = os.path.join("usr", "share", "bcalcosbrand", "liveboot")

ISOLINUX_BIN_REL = os.path.join("usr", "lib", "ISOLINUX", "isolinux.bin")
SYSLINUX_MODULES_REL = os.path.join("usr", "lib", "syslinux", "modules", "bios")
SYSLINUX_MODULES = ("ldlinux.c32", "vesamenu.c32", "chain.c32", "libcom32.c32", "libutil.c32")
GRUB_PACKAGE_MODULES_REL = os.path.join("usr", "lib", "grub", "x86_64-efi")

GRUB_CORE_MODULES = (
    "part_msdos", "part_gpt", "iso9660", "fat", "search", "search_fs_file",
    "search_label", "normal", "configfile", "echo", "boot", "test", "linux",
    "gzio", "gfxterm", "all_video", "efi_gop", "efi_uga",
)

EARLY_CFG_LINES = (
    "search --file --set=root /isolinux/isolinux.cfg",
    "set prefix=($root)/boot/grub",
)

MEMTEST_FILES = ("memtest86+ia32.bin", "memtest86+ia32.efi",
                 "memtest86+x64.bin", "memtest86+x64.efi")


class BootloaderError(Exception):
    pass


def _run(cmd):
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise BootloaderError("%s failed (exit %d): %s"
                              % (cmd[0], proc.returncode, proc.stderr.strip()))
    return proc


def _rootfs(workspace):
    return os.path.join(workspace.path, "staging", "rootfs")


def _payload_dir(rootfs):
    path = os.path.join(rootfs, LIVEBOOT_PAYLOAD_REL)
    if not os.path.isdir(path):
        raise BootloaderError("live-boot payload missing in staging: " + path)
    return path


def _copy_payload_tree(src, dst):
    if not os.path.isdir(src):
        raise BootloaderError("payload section missing: " + src)
    shutil.copytree(src, dst, symlinks=True, dirs_exist_ok=True)


def build_bios_tree(iso_dir, rootfs):
    dest = os.path.join(iso_dir, "isolinux")
    os.makedirs(dest, exist_ok=True)
    bin_src = os.path.join(rootfs, ISOLINUX_BIN_REL)
    if not os.path.isfile(bin_src):
        raise BootloaderError("isolinux.bin not found in staged system: " + bin_src)
    shutil.copy2(bin_src, os.path.join(dest, "isolinux.bin"))
    mods_dir = os.path.join(rootfs, SYSLINUX_MODULES_REL)
    for mod in SYSLINUX_MODULES:
        src = os.path.join(mods_dir, mod)
        if not os.path.isfile(src):
            raise BootloaderError("syslinux module not found in staged system: " + src)
        shutil.copy2(src, os.path.join(dest, mod))
    payload = _payload_dir(rootfs)
    _copy_payload_tree(os.path.join(payload, "isolinux"), dest)
    return dest


def _build_bootx64(rootfs, workdir):
    early_cfg = os.path.join(workdir, "early-grub.cfg")
    with open(early_cfg, "w") as fh:
        fh.write("\n".join(EARLY_CFG_LINES) + "\n")
    modules_dir = os.path.join(rootfs, GRUB_PACKAGE_MODULES_REL)
    out = os.path.join(workdir, "bootx64.efi")
    _run(["grub-mkimage", "-O", "x86_64-efi", "-C", "xz", "-p", "/boot/grub",
          "-c", early_cfg, "-d", modules_dir, "-o", out]
         + list(GRUB_CORE_MODULES))
    return out


def build_uefi_tree(iso_dir, rootfs):
    boot_grub = os.path.join(iso_dir, "boot", "grub")
    efi_boot = os.path.join(iso_dir, "efi", "boot")
    os.makedirs(boot_grub, exist_ok=True)
    os.makedirs(efi_boot, exist_ok=True)
    payload = _payload_dir(rootfs)
    _copy_payload_tree(os.path.join(payload, "grub"), boot_grub)
    modules_src = os.path.join(rootfs, GRUB_PACKAGE_MODULES_REL)
    if not os.path.isdir(modules_src):
        raise BootloaderError("staged grub module directory missing: " + modules_src)
    _copy_payload_tree(modules_src, os.path.join(boot_grub, "x86_64-efi"))
    with tempfile.TemporaryDirectory() as workdir:
        bootx64 = _build_bootx64(rootfs, workdir)
        img = os.path.join(boot_grub, "efiboot.img")
        _run(["mkdosfs", "-C", img, "1440"])
        _run(["mmd", "-i", img, "::/EFI", "::/EFI/boot"])
        _run(["mcopy", "-i", img, bootx64, "::/EFI/boot/bootx64.efi"])
        shutil.copy2(bootx64, os.path.join(boot_grub, "grubx64.efi"))
        shutil.copy2(bootx64, os.path.join(efi_boot, "bootx64.efi"))
    return boot_grub


def write_pkglist(iso_dir, rootfs):
    proc = _run(["dpkg-query", "--root=" + rootfs, "-W",
                 "-f=${Package}\t${Version}\n"])
    entries = sorted(ln for ln in proc.stdout.splitlines() if ln.strip())
    if not entries:
        raise BootloaderError("dpkg-query returned no packages for staged rootfs")
    stamp = time.strftime("%Y%m%d_%H%M")
    out_dir = os.path.join(iso_dir, "pkglist_bcalcos-" + stamp)
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "package_list"), "w") as fh:
        fh.write("\n".join(entries) + "\n")
    return out_dir


def copy_memtest(iso_dir, rootfs):
    live_dir = os.path.join(iso_dir, "live")
    os.makedirs(live_dir, exist_ok=True)
    boot_src = os.path.join(rootfs, "boot")
    copied = []
    if os.path.isdir(boot_src):
        for name in os.listdir(boot_src):
            if name.startswith("memtest"):
                shutil.copy2(os.path.join(boot_src, name), os.path.join(live_dir, name))
                copied.append(name)
    missing = [m for m in MEMTEST_FILES if m not in copied]
    if missing:
        raise BootloaderError("reference memtest files missing from staged /boot: "
                              + ", ".join(missing))
    return sorted(copied)


def build_boot_tree(workspace):
    iso_dir = os.path.join(workspace.path, "staging", "iso")
    rootfs = _rootfs(workspace)
    bios = build_bios_tree(iso_dir, rootfs)
    uefi = build_uefi_tree(iso_dir, rootfs)
    pkglist = write_pkglist(iso_dir, rootfs)
    memtest = copy_memtest(iso_dir, rootfs)
    return {"bios": bios, "uefi": uefi, "pkglist": pkglist,
            "memtest": memtest, "iso_dir": iso_dir}