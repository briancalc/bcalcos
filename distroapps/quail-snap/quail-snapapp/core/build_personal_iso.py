import os
import shutil
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import bootloader
from core import capture_personal as cap
from core import export_personal as exp
from core import home_personal as homep
from core import initramfs
from core import iso_builder
from core import preflight_personal as pfp
from core import sanitize_personal as san
from core import squashfs
from core import verify
from core import workspace_personal as wsp

EXPORT_DIR = exp.EXPORT_DIR
GPG_HOME_NAME = "gnupg-home"
HOST_BOOT_DIR = "/boot"


class PersonalBuildError(Exception):
    pass


def _build_timestamp():
    return time.strftime("%Y%m%d-%H%M%S")


def _rootfs(ws_obj):
    return os.path.join(ws_obj.path, "staging", "rootfs")


def _print_check(result):
    print("[%s] %s: %s" % (result.status.upper(), result.name, result.detail))


def _cleanup_reason(cleanup_error):
    if cleanup_error is None:
        return "workspace retained at 0700"
    return "%s: %s" % (type(cleanup_error).__name__, cleanup_error)


def _running_kernel_version():
    return os.uname().release


def _grub_file_path():
    return shutil.which("grub-file")


def _kernel_format_ok(path):
    grub = _grub_file_path()
    if grub is None:
        return None
    proc = subprocess.run([grub, "--is-x86-linux", path],
                          capture_output=True, text=True)
    return proc.returncode == 0


def _verify_kernel_reference():
    version = _running_kernel_version()
    host_kernel = os.path.join(HOST_BOOT_DIR, "vmlinuz-" + version)
    if os.path.islink(host_kernel) or not os.path.isfile(host_kernel):
        raise PersonalBuildError(
            "kernel integrity [original kernel]: installed kernel for running "
            "version %s is missing, a symlink, or not a regular file: %s"
            % (version, host_kernel))
    fmt = _kernel_format_ok(host_kernel)
    if fmt is None:
        print("kernel integrity [original kernel]: grub-file not available; "
              "kernel format validation skipped (SHA-256 still recorded)")
    elif not fmt:
        raise PersonalBuildError(
            "kernel integrity [original kernel]: %s is not a valid x86 Linux "
            "kernel (grub-file --is-x86-linux failed)" % host_kernel)
    digest = san.sha256_file(host_kernel)
    print("kernel integrity [original kernel]: OK %s sha256=%s" % (host_kernel, digest))
    return version, digest


def _verify_kernel_at_stage(stage, path, expected_digest, check_format=False):
    if os.path.islink(path) or not os.path.isfile(path):
        raise PersonalBuildError(
            "kernel integrity [%s]: %s is missing, a symlink, or not a regular file "
            "(expected sha256 %s)" % (stage, path, expected_digest))
    actual = san.sha256_file(path)
    if actual != expected_digest:
        raise PersonalBuildError(
            "kernel integrity [%s]: sha256 mismatch for %s: expected %s, actual %s"
            % (stage, path, expected_digest, actual))
    if check_format and _kernel_format_ok(path) is False:
        raise PersonalBuildError(
            "kernel integrity [%s]: %s is not a valid x86 Linux kernel "
            "(grub-file --is-x86-linux failed; expected sha256 %s, actual %s)"
            % (stage, path, expected_digest, actual))
    print("kernel integrity [%s]: OK %s sha256=%s" % (stage, path, actual))
    return actual


def _extract_iso_member(iso_path, member, dest):
    proc = subprocess.run(["xorriso", "-osirrox", "on", "-indev", iso_path,
                           "-extract", member, dest], capture_output=True, text=True)
    if proc.returncode != 0:
        raise PersonalBuildError(
            "kernel integrity: cannot extract %s from ISO %s (xorriso exit %d): %s"
            % (member, iso_path, proc.returncode, proc.stderr.strip()))


ISO_LEVEL_ARGS = ("-iso-level", "3")


def _insert_iso_level(cmd, iso_dir):
    if not cmd or cmd[-1] != iso_dir:
        raise iso_builder.IsoBuildError(
            "cannot add -iso-level 3: the xorriso command from build_iso_command() "
            "must be nonempty and end with the source directory %s (final argument: %r)"
            % (iso_dir, cmd[-1] if cmd else None))
    cmd[len(cmd) - 1:len(cmd) - 1] = list(ISO_LEVEL_ARGS)
    return cmd


def _personal_iso_command(iso_dir, output, mbr_template, modification_date=None):
    cmd = iso_builder.build_iso_command(iso_dir, output, mbr_template, modification_date)
    return _insert_iso_level(cmd, iso_dir)


def _assert_personal_iso_command(cmd, iso_dir):
    if not cmd or cmd[-1] != iso_dir:
        raise iso_builder.IsoBuildError(
            "Personal xorriso command must be nonempty and end with the source "
            "directory %s (final argument: %r)"
            % (iso_dir, cmd[-1] if cmd else None))
    if cmd[-3:-1] != list(ISO_LEVEL_ARGS):
        raise iso_builder.IsoBuildError(
            "Personal xorriso command is missing '-iso-level 3' immediately before "
            "the source directory %s" % iso_dir)


def _build_iso_personal(ws_obj, output=None, modification_date=None):
    iso_dir = os.path.join(ws_obj.path, "staging", "iso")
    if not os.path.isdir(iso_dir):
        raise iso_builder.IsoBuildError("ISO tree missing: " + iso_dir)
    if output is None:
        output = os.path.join(ws_obj.path, "quail-snap.iso")
    if not ws_obj.contains(output):
        raise iso_builder.IsoBuildError("output outside workspace: " + output)
    if os.path.lexists(output):
        ws_obj.remove(output)
    mbr_template = os.path.join(ws_obj.path, "staging", "rootfs", iso_builder.ISOHDPFX_REL)
    if not os.path.isfile(mbr_template):
        raise iso_builder.IsoBuildError("isohdpfx.bin not found in staged system, "
                                        "expected at: " + mbr_template)
    cmd = _personal_iso_command(iso_dir, output, mbr_template, modification_date)
    _assert_personal_iso_command(cmd, iso_dir)
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise iso_builder.IsoBuildError("xorriso failed (exit %d): %s"
                                        % (proc.returncode, proc.stderr.strip()))
    if not os.path.isfile(output):
        raise iso_builder.IsoBuildError("xorriso produced no output at " + output)
    return output


def build_personal(passphrase_fd, workspace_root=None, export_dir=EXPORT_DIR):
    exp.validate_passphrase_fd(passphrase_fd)
    timestamp = _build_timestamp()
    wsp.init_personal()
    report = pfp.run_preflight(workspace_root)
    for r in report.results:
        _print_check(r)
    if not report.ok:
        raise PersonalBuildError("initial preflight failed")
    kernel_version, kernel_ref_digest = _verify_kernel_reference()
    ws_obj = None
    lock = None
    build_error = None
    cleanup_ok = True
    cleanup_error = None
    try:
        ws_obj, lock = wsp.create_personal_workspace(root=workspace_root)
        print("workspace:", ws_obj.path)
        creator_pw = homep._invoking_user()
        creator = creator_pw.pw_name
        homep.creator_home(creator_pw)
        cap.capture_root(ws_obj, export_dir, workspace_root=workspace_root)
        print("capture: root filesystem OK")
        staged_kernel = os.path.join(_rootfs(ws_obj), "boot", "vmlinuz-" + kernel_version)
        _verify_kernel_at_stage("after root capture", staged_kernel, kernel_ref_digest)
        cap.capture_home(ws_obj, export_dir, workspace_root=workspace_root,
                         creator=creator)
        print("capture: /home OK")
        san.sanitize(ws_obj)
        print("sanitize: OK")
        prep = homep.prepare(ws_obj, creator=creator)
        print("home preparation: live autologin as %s" % prep.get("live_user", creator))
        san.verify_preservation(ws_obj, creator)
        print("preservation verification: OK")
        initrd, version = initramfs.build_initramfs(ws_obj, release=kernel_version)
        initrd_check = initramfs.verify_initramfs(initrd, version)
        if not initrd_check:
            raise PersonalBuildError("initramfs verification returned an unsuccessful "
                                     "result")
        print("initramfs:", initrd, version, initrd_check)
        initramfs.assert_no_mounts_under(ws_obj)
        print("mount-free staging tree: OK")
        space = pfp.check_space_after_capture(_rootfs(ws_obj), workspace_root)
        _print_check(space)
        if space.status == pfp.FAIL:
            raise PersonalBuildError("post-capture disk-space check failed: "
                                     + space.detail)
        squashfs.build_squashfs(ws_obj, version)
        print("squashfs: OK")
        iso_live_kernel = os.path.join(ws_obj.path, "staging", "iso", "live", "vmlinuz")
        _verify_kernel_at_stage("after squashfs preparation", iso_live_kernel,
                                kernel_ref_digest)
        bootloader.build_boot_tree(ws_obj)
        print("boot tree: OK")
        _verify_kernel_at_stage("after boot-tree construction", iso_live_kernel,
                                kernel_ref_digest)
        iso = _build_iso_personal(ws_obj)
        print("ISO:", iso)
        with tempfile.TemporaryDirectory() as tmp:
            member = os.path.join(tmp, "vmlinuz")
            _extract_iso_member(iso, "/live/vmlinuz", member)
            _verify_kernel_at_stage("final ISO member /live/vmlinuz", member,
                                    kernel_ref_digest, check_format=True)
        iso_check = verify.verify_iso(iso)
        if not iso_check:
            raise PersonalBuildError("ISO verification returned an unsuccessful result")
        print(iso_check)
        gnupg_home = os.path.join(ws_obj.path, GPG_HOME_NAME)
        exported = exp.export(ws_obj, iso, passphrase_fd, timestamp,
                              export_dir=export_dir, gnupg_home=gnupg_home)
    except BaseException as exc:
        build_error = exc
    finally:
        if ws_obj is not None:
            try:
                cleanup_ok = wsp.finalize_personal(ws_obj, lock, retain=False)
            except BaseException as exc:
                cleanup_ok = False
                cleanup_error = exc
    if build_error is not None:
        if ws_obj is not None and not cleanup_ok:
            raise PersonalBuildError(
                "build failed (%s: %s) and secure workspace cleanup was unsuccessful "
                "(%s); workspace retained: %s"
                % (type(build_error).__name__, build_error,
                   _cleanup_reason(cleanup_error), ws_obj.path)) from build_error
        raise build_error
    if not cleanup_ok:
        print("WARNING: encrypted export was produced but secure workspace cleanup "
              "failed (%s)" % _cleanup_reason(cleanup_error), file=sys.stderr)
        print("artifact: %s" % exported["artifact"], file=sys.stderr)
        print("sidecar: %s" % exported["sidecar"], file=sys.stderr)
        print("sha256: %s" % exported["sha256"], file=sys.stderr)
        print("retained workspace (unencrypted intermediate ISO may remain): %s"
              % ws_obj.path, file=sys.stderr)
        print("The Personal build is NOT securely complete.", file=sys.stderr)
        raise PersonalBuildError(
            "encrypted export was produced at %s (sidecar %s, sha256 %s), but secure "
            "workspace cleanup failed (%s); the unencrypted intermediate ISO may remain "
            "in the retained workspace %s; the build is not securely complete"
            % (exported["artifact"], exported["sidecar"], exported["sha256"],
               _cleanup_reason(cleanup_error), ws_obj.path))
    print("exported:", exported["artifact"])
    print("sidecar:", exported["sidecar"])
    print("sha256:", exported["sha256"])
    return {"artifact": exported["artifact"], "sidecar": exported["sidecar"],
            "sha256": exported["sha256"], "owner": exported["owner"],
            "timestamp": timestamp}