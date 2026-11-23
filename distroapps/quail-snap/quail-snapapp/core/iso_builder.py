import os
import subprocess
import time

VOLUME_ID = "liveiso"
ISOHDPFX_REL = os.path.join("usr", "lib", "ISOLINUX", "isohdpfx.bin")


class IsoBuildError(Exception):
    pass


def build_iso_command(iso_dir, output, mbr_template, modification_date=None):
    if modification_date is None:
        modification_date = time.strftime("%Y%m%d%H%M%S00")
    return [
        "xorriso", "-as", "mkisofs",
        "-o", output,
        "-V", VOLUME_ID,
        "--modification-date=" + modification_date,
        "-isohybrid-mbr", mbr_template,
        "-partition_cyl_align", "on",
        "-partition_offset", "16",
        "--mbr-force-bootable",
        "-iso_mbr_part_type", "0x00",
        "-c", "/isolinux/boot.cat",
        "-b", "/isolinux/isolinux.bin",
        "-no-emul-boot", "-boot-load-size", "4", "-boot-info-table",
        "-eltorito-alt-boot",
        "-e", "/boot/grub/efiboot.img",
        "-no-emul-boot", "-boot-load-size", "2880",
        "-isohybrid-gpt-basdat",
        iso_dir,
    ]


def build_iso(workspace, output=None, modification_date=None):
    iso_dir = os.path.join(workspace.path, "staging", "iso")
    if not os.path.isdir(iso_dir):
        raise IsoBuildError("ISO tree missing: " + iso_dir)
    if output is None:
        output = os.path.join(workspace.path, "quail-snap.iso")
    if not workspace.contains(output):
        raise IsoBuildError("output outside workspace: " + output)
    if os.path.lexists(output):
        workspace.remove(output)
    mbr_template = os.path.join(workspace.path, "staging", "rootfs", ISOHDPFX_REL)
    if not os.path.isfile(mbr_template):
        raise IsoBuildError("isohdpfx.bin not found in staged system, expected at: "
                            + mbr_template)
    cmd = build_iso_command(iso_dir, output, mbr_template, modification_date)
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise IsoBuildError("xorriso failed (exit %d): %s"
                            % (proc.returncode, proc.stderr.strip()))
    if not os.path.isfile(output):
        raise IsoBuildError("xorriso produced no output at " + output)
    return output