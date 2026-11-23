import os
import shutil
import subprocess
import time

LIVE_USER = "isouser"
LIVE_USER_UID = 1001
LIVE_USER_GID = 1001
LIVE_USER_PASSWORD = "1234"

LAUNCHER_DESKTOP_FILES = (
    "bcalcos_welcome.desktop",
    "bcalcos-installer.desktop",
)


class HomeError(Exception):
    pass


def _rootfs(workspace):
    return os.path.join(workspace.path, "staging", "rootfs")


def _lookup_uid_gid(rootfs, user):
    passwd = os.path.join(rootfs, "etc", "passwd")
    try:
        with open(passwd, "r") as fh:
            for line in fh:
                fields = line.split(":")
                if len(fields) >= 4 and fields[0] == user:
                    return int(fields[2]), int(fields[3])
    except OSError as exc:
        raise HomeError("cannot read staged /etc/passwd: %s" % exc)
    return None


def _yescrypt_hash(password):
    proc = subprocess.run(["mkpasswd", "-m", "yescrypt", password],
                          capture_output=True, text=True)
    if proc.returncode != 0:
        raise HomeError("cannot generate yescrypt hash: " + proc.stderr.strip())
    return proc.stdout.strip()


def ensure_live_user(rootfs, user=LIVE_USER, uid=LIVE_USER_UID, gid=LIVE_USER_GID):
    found = _lookup_uid_gid(rootfs, user)
    if found is not None:
        return found
    passwd = os.path.join(rootfs, "etc", "passwd")
    group = os.path.join(rootfs, "etc", "group")
    for path, field, value in ((passwd, 2, uid), (group, 2, gid)):
        with open(path, "r") as fh:
            for line in fh:
                fields = line.split(":")
                if len(fields) > field and fields[field] == str(value) and fields[0] != user:
                    raise HomeError("uid/gid %d already used by account %r; refusing to "
                                    "synthesize %s with a colliding id" % (value, fields[0], user))
    lastchg = int(time.time() // 86400)
    shadow_fields = [user, _yescrypt_hash(LIVE_USER_PASSWORD), str(lastchg),
                     "0", "99999", "7", "", "", ""]
    entries = {
        "etc/passwd": "%s:x:%d:%d:%s,,,:/home/%s:/bin/bash\n" % (user, uid, gid, user, user),
        "etc/shadow": ":".join(shadow_fields) + "\n",
        "etc/group": "%s:x:%d:\n" % (user, gid),
        "etc/gshadow": "%s:!::\n" % user,
    }
    for rel, line in entries.items():
        path = os.path.join(rootfs, rel)
        if not os.path.isfile(path):
            raise HomeError("staged account file missing: " + path)
        with open(path, "a") as fh:
            fh.write(line)
    print("SYNTHESIZED live user %s (uid/gid %d) in staged account files" % (user, uid))
    return uid, gid


def _chown_tree(path, uid, gid):
    os.chown(path, uid, gid)
    for dirpath, dirnames, filenames in os.walk(path):
        for name in dirnames + filenames:
            full = os.path.join(dirpath, name)
            if not os.path.islink(full):
                os.chown(full, uid, gid)


def build_home(workspace, user=LIVE_USER):
    rootfs = _rootfs(workspace)
    skel = os.path.join(rootfs, "etc", "skel")
    home = os.path.join(rootfs, "home", user)
    if not workspace.contains(home):
        raise HomeError("home path outside workspace: " + home)
    if not os.path.isdir(skel):
        raise HomeError("staged /etc/skel missing: " + skel)
    if os.path.lexists(home):
        workspace.remove(home)
    shutil.copytree(skel, home, symlinks=True)
    desktop = os.path.join(home, "Desktop")
    if os.path.lexists(desktop):
        workspace.remove(desktop)
    os.makedirs(desktop)
    for name in LAUNCHER_DESKTOP_FILES:
        src = os.path.join(rootfs, "usr", "share", "applications", name)
        if not os.path.isfile(src):
            raise HomeError("launcher not found in staged /usr/share/applications: " + name)
        shutil.copy2(src, os.path.join(desktop, name))
    uid, gid = ensure_live_user(rootfs, user)
    _chown_tree(home, uid, gid)
    return home