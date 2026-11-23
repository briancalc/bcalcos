import datetime
import fcntl
import os
import pwd
import re
import stat
import subprocess

from core import sanitize_personal as sp

EXPORT_DIR = "/home/quail_snapiso"
ARTIFACT_TEMPLATE = "quail-personal-%s.iso.gpg"
TIMESTAMP_RE = re.compile(r"[0-9]{8}-[0-9]{6}")
GPG_FLAGS = ("--symmetric", "--cipher-algo", "AES256", "--batch",
             "--pinentry-mode", "loopback", "--no-symkey-cache")


class ExportPersonalError(Exception):
    pass


def _invoking_user():
    sudo_uid = os.environ.get("SUDO_UID")
    if not sudo_uid or not sudo_uid.isdigit():
        raise ExportPersonalError("cannot resolve invoking user: SUDO_UID is not set")
    try:
        return pwd.getpwuid(int(sudo_uid))
    except KeyError:
        raise ExportPersonalError("cannot resolve invoking user: no account for uid %s"
                                  % sudo_uid)


def _validate_timestamp(timestamp):
    if not isinstance(timestamp, str) or not TIMESTAMP_RE.fullmatch(timestamp):
        raise ExportPersonalError("invalid timestamp (need YYYYMMDD-HHMMSS, ASCII "
                                  "digits): %r" % (timestamp,))
    try:
        moment = datetime.datetime.strptime(timestamp, "%Y%m%d-%H%M%S")
    except ValueError:
        raise ExportPersonalError("invalid calendar date/time: %r" % (timestamp,))
    if moment.strftime("%Y%m%d-%H%M%S") != timestamp:
        raise ExportPersonalError("invalid calendar date/time: %r" % (timestamp,))
    return timestamp


def _validate_passphrase_fd(fd):
    if not isinstance(fd, int) or fd < 0:
        raise ExportPersonalError("invalid passphrase fd: %r" % (fd,))
    try:
        st = os.fstat(fd)
    except OSError as exc:
        raise ExportPersonalError("passphrase fd %r is not open: %s" % (fd, exc))
    if not stat.S_ISFIFO(st.st_mode):
        raise ExportPersonalError("passphrase fd %r is not a pipe" % (fd,))
    if fcntl.fcntl(fd, fcntl.F_GETFL) & os.O_ACCMODE != os.O_RDONLY:
        raise ExportPersonalError("passphrase fd %r is not read-only" % (fd,))
    return fd


def validate_passphrase_fd(fd):
    return _validate_passphrase_fd(fd)


def _validate_gnupg_home(workspace, gnupg_home):
    p = os.path.abspath(gnupg_home)
    if os.path.islink(p):
        raise ExportPersonalError("gnupg_home is a symlink, refusing: " + p)
    ws_real = os.path.realpath(workspace.path)
    real = os.path.realpath(p)
    if real == ws_real or not real.startswith(ws_real + os.sep):
        raise ExportPersonalError("gnupg_home outside workspace: " + p)
    return p


def _validate_iso(workspace, iso_path):
    p = os.path.abspath(iso_path)
    if os.path.islink(p) or not os.path.isfile(p):
        raise ExportPersonalError("ISO missing, not a regular file, or a symlink: " + p)
    ws_real = os.path.realpath(workspace.path)
    real = os.path.realpath(p)
    if real == ws_real or not real.startswith(ws_real + os.sep):
        raise ExportPersonalError("ISO outside workspace: " + p)
    return real


def _prepare_export_dir(export_dir, pw):
    if os.path.islink(export_dir):
        raise ExportPersonalError("export directory is a symlink, refusing: "
                                  + export_dir)
    if os.path.isdir(export_dir):
        if not os.access(export_dir, os.W_OK | os.X_OK):
            raise ExportPersonalError("export directory not usable: " + export_dir)
        return
    os.makedirs(export_dir)
    os.chown(export_dir, pw.pw_uid, pw.pw_gid)


def _open_exclusive(path, mode):
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    return os.open(path, flags, mode)


def export(workspace, iso_path, passphrase_fd, timestamp, export_dir=EXPORT_DIR,
           gnupg_home=None):
    ts = _validate_timestamp(timestamp)
    iso = _validate_iso(workspace, iso_path)
    _validate_passphrase_fd(passphrase_fd)
    if gnupg_home is not None:
        gnupg_home = _validate_gnupg_home(workspace, gnupg_home)
    pw = _invoking_user()
    _prepare_export_dir(export_dir, pw)
    gpg_name = ARTIFACT_TEMPLATE % ts
    sidecar_name = gpg_name + ".sha256"
    dest_gpg = os.path.join(export_dir, gpg_name)
    dest_side = os.path.join(export_dir, sidecar_name)
    for p in (dest_gpg, dest_side):
        if os.path.lexists(p):
            raise ExportPersonalError("export destination already exists, refusing to "
                                      "overwrite: " + p)
    created = []
    try:
        art_fd = _open_exclusive(dest_gpg, 0o600)
        created.append(dest_gpg)
        cmd = (["gpg"] + list(GPG_FLAGS)
               + ["--passphrase-fd", str(passphrase_fd), "--output", "-", iso])
        env = None
        if gnupg_home is not None:
            os.makedirs(gnupg_home, exist_ok=True)
            os.chmod(gnupg_home, 0o700)
            env = dict(os.environ)
            env["GNUPGHOME"] = gnupg_home
        art_fh = os.fdopen(art_fd, "wb")
        try:
            proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=art_fh,
                                    stderr=subprocess.PIPE, close_fds=True,
                                    pass_fds=(passphrase_fd,), env=env)
        finally:
            art_fh.close()
        gpg_stderr = proc.communicate()[1]
        if proc.returncode != 0:
            raise ExportPersonalError("gpg encryption failed (exit %d): %s"
                                      % (proc.returncode,
                                         gpg_stderr.decode(errors="replace").strip()))
        if os.lstat(dest_gpg).st_size == 0:
            raise ExportPersonalError("gpg produced an empty artifact")
        os.chown(dest_gpg, pw.pw_uid, pw.pw_gid)
        os.chmod(dest_gpg, 0o600)
        digest = sp.sha256_file(dest_gpg)
        side_fd = _open_exclusive(dest_side, 0o644)
        created.append(dest_side)
        try:
            os.write(side_fd, ("%s  %s\n" % (digest, gpg_name)).encode())
        finally:
            os.close(side_fd)
        os.chown(dest_side, pw.pw_uid, pw.pw_gid)
        os.chmod(dest_side, 0o644)
        proc = subprocess.run(["sha256sum", "-c", sidecar_name], cwd=export_dir,
                              capture_output=True, text=True)
        if proc.returncode != 0:
            raise ExportPersonalError("exported pair failed sha256sum -c: "
                                      + proc.stderr.strip())
        return {"artifact": dest_gpg, "sidecar": dest_side, "sha256": digest,
                "owner": pw.pw_name}
    except BaseException as exc:
        cleanup_errors = []
        for p in created:
            try:
                if os.path.lexists(p) and not os.path.islink(p):
                    os.remove(p)
            except OSError as rm_exc:
                cleanup_errors.append("%s: %s" % (p, rm_exc))
        if cleanup_errors:
            raise ExportPersonalError(
                "export failed (%s: %s) and cleanup of created files was "
                "unsuccessful: %s"
                % (type(exc).__name__, exc, "; ".join(cleanup_errors))) from exc
        raise