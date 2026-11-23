import hashlib
import os
import subprocess
import tempfile

REQUIRED_ISO_PATHS = (
    "/live/filesystem.squashfs",
    "/live/vmlinuz",
    "/live/initrd.img",
    "/isolinux/isolinux.bin",
    "/isolinux/vesamenu.c32",
    "/isolinux/splash.png",
    "/isolinux/isolinux.cfg",
    "/isolinux/menu.cfg",
    "/isolinux/live.cfg",
    "/isolinux/stdmenu.cfg",
    "/boot/grub/efiboot.img",
    "/boot/grub/grub.cfg",
    "/boot/grub/x86_64-efi/grub.cfg",
    "/efi/boot/bootx64.efi",
)
VOLUME_ID = "liveiso"
EFIBOOT_ONLY_MEMBER = "EFI/boot/bootx64.efi"
EARLY_CFG_MARKERS = (
    b"search --file --set=root /isolinux/isolinux.cfg",
    b"set prefix=($root)/boot/grub",
)


class VerifyError(Exception):
    pass


def _run(cmd):
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise VerifyError("%s failed (exit %d): %s"
                          % (cmd[0], proc.returncode, proc.stderr.strip()))
    return proc.stdout


def _extract(iso_path, iso_member, dest):
    _run(["xorriso", "-osirrox", "on", "-indev", iso_path,
          "-extract", iso_member, dest])
    return dest


def _check_paths(iso_path):
    listing = _run(["xorriso", "-indev", iso_path, "-find", "/"])
    paths = {line.strip().strip("'\"").rstrip("/")
             for line in listing.splitlines() if line.strip()}
    missing = [p for p in REQUIRED_ISO_PATHS if p not in paths]
    if missing:
        raise VerifyError("ISO missing required paths: " + ", ".join(missing))
    if not any("pkglist_bcalcos-" in p for p in paths):
        raise VerifyError("ISO missing pkglist_bcalcos-* directory")
    module_dir = [p for p in paths if p.startswith("/boot/grub/x86_64-efi/")]
    if not module_dir:
        raise VerifyError("ISO /boot/grub/x86_64-efi/ module directory is empty")
    return True


def _check_boot_records(iso_path):
    out = _run(["xorriso", "-indev", iso_path, "-report_el_torito", "plain"])
    if "isolinux.bin" not in out or "efiboot.img" not in out:
        raise VerifyError("El Torito boot records incomplete (BIOS/UEFI entries missing)")
    return True


def _check_system_area(iso_path):
    out = _run(["xorriso", "-indev", iso_path, "-report_system_area", "plain"])
    if "isohybrid-gpt-basdat" not in out and "GPT" not in out:
        raise VerifyError("no GPT/isohybrid-gpt-basdat system area in ISO")
    return True


def _check_volume_id(iso_path):
    out = _run(["xorriso", "-indev", iso_path, "-pvd_info"])
    if VOLUME_ID not in out:
        raise VerifyError("volume id is not %r" % VOLUME_ID)
    return True


def _check_efiboot_img(iso_path):
    with tempfile.TemporaryDirectory() as tmp:
        img = _extract(iso_path, "/boot/grub/efiboot.img", os.path.join(tmp, "efiboot.img"))
        outdir = os.path.join(tmp, "fat")
        os.makedirs(outdir)
        _run(["mcopy", "-i", img, "-s", "::/", outdir])
        files = sorted(os.path.relpath(os.path.join(dp, f), outdir)
                       for dp, _dn, fn in os.walk(outdir) for f in fn)
        if files != [EFIBOOT_ONLY_MEMBER]:
            raise VerifyError("efiboot.img must contain exactly %s, found: %s"
                              % (EFIBOOT_ONLY_MEMBER, ", ".join(files)))
        data = open(os.path.join(outdir, EFIBOOT_ONLY_MEMBER), "rb").read()
    for marker in EARLY_CFG_MARKERS:
        if marker not in data:
            raise VerifyError("bootx64.efi missing early-config marker: %r" % marker)
    return True


def _check_isolinux_live_cfg(iso_path):
    with tempfile.TemporaryDirectory() as tmp:
        cfg = _extract(iso_path, "/isolinux/live.cfg", os.path.join(tmp, "live.cfg"))
        lines = open(cfg, "r").read().splitlines()
    active = [ln for ln in lines if ln.startswith("label ")]
    if len(active) != 1:
        raise VerifyError("live.cfg should have exactly one active entry, found %d" % len(active))
    memtest_lines = [ln for ln in lines if "memtest" in ln]
    if any(not ln.lstrip().startswith("#") for ln in memtest_lines):
        raise VerifyError("memtest entries must be commented out")
    return True


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_iso(iso_path):
    if not os.path.isfile(iso_path):
        raise VerifyError("ISO not found: " + iso_path)
    _check_paths(iso_path)
    _check_boot_records(iso_path)
    _check_system_area(iso_path)
    _check_volume_id(iso_path)
    _check_efiboot_img(iso_path)
    _check_isolinux_live_cfg(iso_path)
    digest = sha256_file(iso_path)
    with open(iso_path + ".sha256", "w") as fh:
        fh.write("%s  %s\n" % (digest, os.path.basename(iso_path)))
    return {"paths": True, "el_torito": True, "system_area_gpt": True,
            "volume_id": VOLUME_ID, "efiboot_img": True,
            "live_cfg": True, "sha256": digest}