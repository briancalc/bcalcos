import os
import pwd
import shutil
import subprocess
import sys
import time

sys.path.insert(0, "/usr/lib/quail-snapapp")

from core import workspace, preflight, capture, home, brand, sanitize
from core import initramfs, squashfs, bootloader, iso_builder, verify

EXPORT_DIR = "/home/quail_snapiso"


def _invoking_user():
    sudo_uid = os.environ.get("SUDO_UID")
    if not sudo_uid or not sudo_uid.isdigit():
        raise RuntimeError("cannot resolve invoking user: SUDO_UID is not set")
    try:
        return pwd.getpwuid(int(sudo_uid))
    except KeyError:
        raise RuntimeError("cannot resolve invoking user: no account for uid %s" % sudo_uid)


def _prepare_export_dir(pw):
    if os.path.isdir(EXPORT_DIR):
        if not os.access(EXPORT_DIR, os.W_OK | os.X_OK):
            raise RuntimeError("export directory not usable: " + EXPORT_DIR)
        return
    os.makedirs(EXPORT_DIR)
    os.chown(EXPORT_DIR, pw.pw_uid, pw.pw_gid)


def export_iso(iso_path):
    created = []
    try:
        with open(iso_path + ".sha256", "r") as fh:
            digest = fh.read().split()[0]
        proc = subprocess.run(["sha256sum", "-c", os.path.basename(iso_path) + ".sha256"],
                              cwd=os.path.dirname(iso_path),
                              capture_output=True, text=True)
        if proc.returncode != 0:
            raise RuntimeError("workspace ISO failed checksum validation: "
                               + proc.stderr.strip())
        date = time.strftime("%Y%m%d-%H%M%S")
        iso_name = "quail-snap-%s.iso" % date
        sidecar_name = iso_name + ".sha256"
        pw = _invoking_user()
        _prepare_export_dir(pw)
        dest_iso = os.path.join(EXPORT_DIR, iso_name)
        dest_sidecar = os.path.join(EXPORT_DIR, sidecar_name)
        if os.path.lexists(dest_iso) or os.path.lexists(dest_sidecar):
            raise RuntimeError("export destination already exists, refusing to overwrite: "
                               + dest_iso)
        created.append(dest_iso)
        shutil.copy2(iso_path, dest_iso)
        os.chown(dest_iso, pw.pw_uid, pw.pw_gid)
        if verify.sha256_file(dest_iso) != digest:
            raise RuntimeError("exported ISO digest does not match workspace digest")
        created.append(dest_sidecar)
        with open(dest_sidecar, "w") as fh:
            fh.write("%s  %s\n" % (digest, iso_name))
        os.chown(dest_sidecar, pw.pw_uid, pw.pw_gid)
        proc = subprocess.run(["sha256sum", "-c", sidecar_name],
                              cwd=EXPORT_DIR, capture_output=True, text=True)
        if proc.returncode != 0:
            raise RuntimeError("exported pair failed sha256sum -c: " + proc.stderr.strip())
        return dest_iso
    except BaseException:
        for path in created:
            try:
                if os.path.lexists(path):
                    os.remove(path)
            except OSError:
                pass
        raise


def main():
    lock = workspace.kickoff_cleanup()
    w = workspace.create_workspace()
    print("workspace:", w.path)
    exported = None
    try:
        pw = _invoking_user()
        creator = pw.pw_name
        workspace.init()
        report = preflight.run_preflight()
        for r in report.results:
            print("[%s] %s: %s" % (r.status.upper(), r.name, r.detail))
        if not report.ok:
            raise SystemExit("preflight failed")
        capture.capture(w, EXPORT_DIR)
        sanitize.sanitize(w, creator=creator)
        home.build_home(w)
        brand.inject_brand(w)
        initrd, version = initramfs.build_initramfs(w)
        print("initramfs:", initrd, version, initramfs.verify_initramfs(initrd, version))
        initramfs.assert_no_mounts_under(w)
        print("mount-free staging tree: OK")
        brand.assert_required_brand_files(w)
        squashfs.build_squashfs(w, version)
        bootloader.build_boot_tree(w)
        iso = iso_builder.build_iso(w)
        print("ISO:", iso)
        print(verify.verify_iso(iso))
        exported = export_iso(iso)
        print("exported:", exported)
    finally:
        workspace.finalize(w, exported, lock)


if __name__ == "__main__":
    main()