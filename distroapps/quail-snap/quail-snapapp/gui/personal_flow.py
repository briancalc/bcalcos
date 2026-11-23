import fcntl
import os
import sys
import threading
import time
import tkinter as tk
import traceback

from core import build_personal_iso as bpi
from core import export_personal as exp
from core import workspace_personal as wsp


class PersonalFlowError(Exception):
    pass


def _mw():
    from gui import main_window
    return main_window


CONFIRM_TITLE = "Personal ISO - sensitive data confirmation"
CONFIRM_TEXT = (
    "A snapshot of this machine's personal data will be captured.\n\n"
    "The backup preserves your personal files, accounts, credentials and password "
    "hashes, application settings, machine identity, network and VPN configuration "
    "and credentials, SSH host keys, and system logs. Only approved transient "
    "runtime information, caches, selected state files, and regenerable package "
    "indexes are excluded.\n\n"
    "Other user accounts are retained, but their personal files are not included "
    "in this Personal ISO.\n\n"
    "The complete ISO is protected by mandatory GnuPG AES-256 encryption. The "
    "unencrypted intermediate ISO remains inside the Personal build workspace and is "
    "never exported. The encrypted ISO requires the passphrase for recovery.\n\n"
    "If you lose the encryption passphrase, you will not be able to decrypt or "
    "recover this backup.\n\n"
    "Please preserve the passphrase securely, separately from the encrypted ISO."
)

SUCCESS_WARNING = (
    "The Personal ISO contains captured personal data.\n\n"
    "The encryption passphrase is required to recover this backup. If you lose the "
    "passphrase, the backup is unrecoverable.\n\n"
    "Protect the encrypted artifact and the passphrase separately. The artifact is "
    "only as safe as its passphrase and its storage location."
)

CLEANUP_WARNING_MARKER = ("WARNING: encrypted export was produced but secure "
                          "workspace cleanup failed")

DIAGNOSTIC_LOG_LIMIT = 2000
DIAGNOSTIC_DIR_NAME = "diagnostics"


def _diagnostic_log_path(root=None):
    if root is None:
        root = wsp.workspace.DEFAULT_ROOT
    name = "personal-build-%s-%d-%s.log" % (
        time.strftime("%Y%m%d-%H%M%S"), os.getpid(), os.urandom(4).hex())
    return os.path.join(root, DIAGNOSTIC_DIR_NAME, name)


def _open_diagnostic_log(root=None):
    path = _diagnostic_log_path(root)
    diag_dir = os.path.dirname(path)
    try:
        os.makedirs(diag_dir, exist_ok=True)
        os.chmod(diag_dir, 0o700)
    except OSError as exc:
        raise PersonalFlowError("cannot prepare diagnostic log directory %s: %s"
                                % (diag_dir, exc))
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags, 0o600)
    except OSError as exc:
        raise PersonalFlowError("cannot open diagnostic log %s: %s" % (path, exc))
    return os.fdopen(fd, "w", encoding="utf-8", errors="backslashreplace"), path

def _purge_old_diagnostic_logs(root=None):
    if root is None:
        root = wsp.workspace.DEFAULT_ROOT
    diag_dir = os.path.join(root, DIAGNOSTIC_DIR_NAME)
    try:
        names = os.listdir(diag_dir)
    except OSError:
        return
    for name in names:
        if name.startswith("personal-build-") and name.endswith(".log"):
            path = os.path.join(diag_dir, name)
            try:
                if os.path.isfile(path) and not os.path.islink(path):
                    os.remove(path)
            except OSError:
                pass

def _bound_detail(detail, log_path=None, limit=DIAGNOSTIC_LOG_LIMIT):
    if len(detail) <= limit:
        return detail
    note = "\n... [truncated: %d more characters" % (len(detail) - limit)
    if log_path:
        note += "; full details in %s" % log_path
    return detail[:limit] + note + "]"


def _passphrase_error(p1, p2):
    if not p1:
        return "Passphrase must not be empty."
    if p1 != p2:
        return "Passphrase entries do not match."
    return None


def _clear_entries(entries):
    for e in entries:
        e.delete(0, "end")


def _complete_passphrase(entries, result_box):
    p1 = entries[0].get()
    p2 = entries[1].get()
    err = _passphrase_error(p1, p2)
    if err:
        return err
    _clear_entries(entries)
    result_box.append(p1)
    return None


def handoff_passphrase(passphrase):
    r, w = os.pipe()
    buf = None
    try:
        if "\n" in passphrase or "\r" in passphrase:
            raise PersonalFlowError("passphrases containing line breaks (CR/LF) are "
                                    "not supported by the passphrase transport; the "
                                    "build was not started")
        try:
            exp.validate_passphrase_fd(r)
        except exp.ExportPersonalError as exc:
            raise PersonalFlowError(str(exc)) from exc
        try:
            buf = bytearray(passphrase.encode("utf-8"))
        except UnicodeError:
            raise PersonalFlowError("passphrase could not be encoded as UTF-8; "
                                    "the build was not started")
        buf.append(0x0A)
        getpipe_sz = getattr(fcntl, "F_GETPIPE_SZ", None)
        if getpipe_sz is None:
            raise PersonalFlowError("pipe capacity query is unavailable on this "
                                    "platform; the build was not started")
        try:
            capacity = fcntl.fcntl(r, getpipe_sz)
        except OSError as exc:
            raise PersonalFlowError("cannot determine pipe capacity: %s" % exc)
        if len(buf) > capacity:
            raise PersonalFlowError(
                "passphrase payload (%d bytes) exceeds the pipe capacity (%d bytes); "
                "the build was not started" % (len(buf), capacity))
        off = 0
        while off < len(buf):
            n = os.write(w, buf[off:])
            if n <= 0:
                raise PersonalFlowError("pipe write made no progress; the build "
                                        "was not started")
            off += n
    except BaseException:
        for fd in (r, w):
            try:
                os.close(fd)
            except OSError:
                pass
        raise
    finally:
        if buf is not None:
            buf[:] = b"\x00" * len(buf)
            del buf
    try:
        os.close(w)
    except OSError as exc:
        try:
            os.close(r)
        except OSError:
            pass
        raise PersonalFlowError("could not close the pipe write end: %s" % exc)
    return r


class _CleanupWarningParser:
    def __init__(self):
        self.seen = False
        self.fields = {}

    def feed(self, line):
        if line.startswith(CLEANUP_WARNING_MARKER):
            self.seen = True
            rest = line[len(CLEANUP_WARNING_MARKER):].strip()
            if rest.startswith("(") and rest.endswith(")"):
                rest = rest[1:-1].strip()
            if rest:
                self.fields["reason"] = rest
            return
        if not self.seen:
            return
        for label in ("artifact:", "sidecar:", "sha256:"):
            if line.startswith(label):
                self.fields[label[:-1]] = line[len(label):].strip()
                return
        if line.startswith("retained workspace") and ":" in line:
            self.fields["workspace"] = line.split(":", 1)[1].strip()


def _classify_failure(exc, parser):
    if parser.seen:
        return {"kind": "cleanup_failure", "exc": exc,
                "warning": dict(parser.fields)}
    return {"kind": "error", "exc": exc}


class _WorkerStreamProxy:
    def __init__(self, real, worker_ident, q, watcher=None, sink=None):
        self._real = real
        self._ident = worker_ident
        self._q = q
        self._watcher = watcher
        self._sink = sink
        self._buf = ""

    def _emit(self, line):
        self._q.put(("log", line, None))
        if self._watcher is not None:
            self._watcher.feed(line)
        if self._sink is not None:
            try:
                self._sink.write(line + "\n")
                self._sink.flush()
            except Exception:
                pass

    def write(self, s):
        if not s:
            return 0
        if threading.get_ident() == self._ident:
            self._buf += s
            while "\n" in self._buf:
                line, self._buf = self._buf.split("\n", 1)
                self._emit(line)
            return len(s)
        return self._real.write(s)

    def flush(self):
        if threading.get_ident() == self._ident:
            if self._buf:
                line, self._buf = self._buf, ""
                self._emit(line)
            return
        self._real.flush()

    def __getattr__(self, name):
        return getattr(self._real, name)


class _StreamRouter:
    def __init__(self, q, watcher=None, sink=None):
        self._q = q
        self._watcher = watcher
        self._sink = sink
        self._saved = None
        self._proxies = []
        self._ident = None

    def install(self):
        saved = (sys.stdout, sys.stderr)
        ident = threading.get_ident()
        proxies = [
            _WorkerStreamProxy(saved[0], ident, self._q, self._watcher, self._sink),
            _WorkerStreamProxy(saved[1], ident, self._q, self._watcher, self._sink),
        ]
        self._saved = saved
        self._ident = ident
        self._proxies = proxies
        sys.stdout, sys.stderr = proxies

    def restore(self):
        if self._saved is None:
            return
        errors = []
        if threading.get_ident() == self._ident:
            for proxy in self._proxies:
                try:
                    proxy.flush()
                except BaseException as exc:
                    errors.append("flush: %s: %s" % (type(exc).__name__, exc))
        sys.stdout, sys.stderr = self._saved
        self._saved = None
        self._proxies = []
        self._ident = None
        if errors:
            raise PersonalFlowError("; ".join(errors))


class PersonalFlow:
    def __init__(self, main):
        self.main = main
        self._shutdown_noted = False
        wsp.init_personal()
        main.root.after(100, self._poll_shutdown)

    def _poll_shutdown(self):
        signum = wsp.pending_signal()
        if signum is not None:
            if self.main._busy:
                if not self._shutdown_noted:
                    self._shutdown_noted = True
                    self.main._append_output(
                        "Shutdown requested (signal %d). The Personal build is being "
                        "allowed to finish so that its workspace can be securely "
                        "cleaned." % signum)
            else:
                sys.exit(128 + signum)
        self.main.root.after(100, self._poll_shutdown)

    def start(self):
        main = self.main
        if main._busy or not main._is_root:
            return
        passphrase = self._ask_passphrase()
        if passphrase is None:
            return
        try:
            if not self._confirm_sensitive_data():
                return
            try:
                read_fd = handoff_passphrase(passphrase)
            except (PersonalFlowError, OSError) as exc:
                main.show_message("Quail Snap Personal ISO", str(exc),
                                  kind="error")
                return
        finally:
            passphrase = None
        main._busy = True
        worker_started = False
        try:
            main._update_button_states()
            main._build_start = time.monotonic()
            main.build_time_label.configure(text="Build Time: 00:00")
            main._timer_after_id = main.root.after(1000, main._tick_build_timer)
            main._set_output(
                "Creating Personal ISO…\n\n"
                "Running the full Personal build pipeline (capture, sanitize, home "
                "preparation, initramfs, squashfs, boot tree, ISO, verify, encrypted "
                "export). This can take a long time."
            )
            worker = threading.Thread(target=self._worker, args=(read_fd,),
                                      name="QuailSnapPersonalBuild")
            worker.start()
            worker_started = True
            main.root.after(100, main._poll_task_queue)
        except BaseException as exc:
            if worker_started:
                self._ensure_completion_path(exc)
            else:
                self._rollback_startup(read_fd, exc)
            return

    def _rollback_startup(self, read_fd, exc):
        main = self.main
        try:
            os.close(read_fd)
        except OSError:
            pass
        main._busy = False
        try:
            main._update_button_states()
        except Exception:
            pass
        try:
            main._stop_build_timer()
        except Exception:
            pass
        main.show_message("Quail Snap Personal ISO",
                          "could not start the Personal build: %s: %s"
                          % (type(exc).__name__, exc), kind="error")

    def _ensure_completion_path(self, exc):
        main = self.main
        try:
            main.root.after(100, main._poll_task_queue)
            return
        except BaseException:
            pass
        try:
            main._poll_task_queue()
        except BaseException:
            pass
        main.show_message(
            "Quail Snap Personal ISO",
            "the Personal build is running, but its completion monitor could not "
            "be scheduled (%s: %s); the outcome cannot be displayed automatically"
            % (type(exc).__name__, exc), kind="error")

    def _worker(self, read_fd, log_root=None):
        parser = _CleanupWarningParser()
        log_fh = None
        log_path = None
        log_error = None
        _purge_old_diagnostic_logs(log_root)
        try:
            log_fh, log_path = _open_diagnostic_log(log_root)
        except Exception as exc:
            log_error = "cannot open diagnostic log: %s: %s" % (type(exc).__name__, exc)
        if log_fh is not None:
            try:
                log_fh.write("Quail Snap Personal build diagnostic log\n")
                log_fh.write("pid %d started %s\n"
                             % (os.getpid(), time.strftime("%Y-%m-%d %H:%M:%S")))
                log_fh.flush()
            except Exception as exc:
                log_error = ("cannot write diagnostic log header: %s: %s"
                             % (type(exc).__name__, exc))
                try:
                    log_fh.close()
                except Exception:
                    pass
                log_fh = None
        router = _StreamRouter(self.main._task_queue, parser, sink=log_fh)
        try:
            router.install()
            result = bpi.build_personal(read_fd)
            outcome = {"kind": "success", "result": result}
        except BaseException as exc:
            if log_fh is not None:
                try:
                    log_fh.write("\n--- failure traceback ---\n")
                    log_fh.write(traceback.format_exc())
                    log_fh.flush()
                except Exception:
                    pass
            outcome = _classify_failure(exc, parser)
        if log_path is not None:
            outcome["log"] = log_path
        if log_error is not None:
            outcome["log_error"] = log_error
        teardown_errors = []
        try:
            os.close(read_fd)
        except OSError as exc:
            teardown_errors.append("closing the passphrase descriptor: %s" % exc)
        try:
            router.restore()
        except BaseException as exc:
            teardown_errors.append("stream restore: %s: %s" % (type(exc).__name__, exc))
        if log_fh is not None:
            try:
                log_fh.close()
            except Exception as exc:
                teardown_errors.append("closing the diagnostic log: %s" % exc)
        if teardown_errors:
            outcome["teardown"] = teardown_errors
        self.main._task_queue.put(("success", outcome, self._on_outcome))

    def _on_outcome(self, outcome):
        teardown = outcome.get("teardown")
        if teardown:
            self.main._append_output("Worker teardown warning: " + "; ".join(teardown))
        if outcome.get("log_error"):
            self.main._append_output("Diagnostic log warning: " + outcome["log_error"])
        kind = outcome.get("kind")
        if kind == "success":
            self._present_success(outcome["result"])
        elif kind == "cleanup_failure":
            self._present_cleanup_failure(outcome)
        else:
            self._present_error(outcome["exc"], outcome.get("log"))

    def _present_success(self, result):
        self.main._append_output("")
        self.main._append_output("Personal build succeeded.")
        self.main._append_output("Encrypted ISO: %s" % result["artifact"])
        self.main._append_output("Checksum sidecar: %s" % result["sidecar"])
        self.main._append_output("SHA-256: %s" % result["sha256"])
        self.main.show_message("Personal ISO created", SUCCESS_WARNING,
                               kind="warning")

    def _present_cleanup_failure(self, outcome):
        w = outcome.get("warning", {})
        expected = ("artifact", "sidecar", "sha256", "workspace")
        missing = [k for k in expected if not w.get(k)]
        lines = []
        if missing:
            lines.append("Warning details incomplete in the build output; missing: "
                         + ", ".join(missing))
        lines.append("ENCRYPTED EXPORT PRODUCED: %s" % (w.get("artifact") or "unknown"))
        lines.append("Checksum sidecar: %s" % (w.get("sidecar") or "unknown"))
        lines.append("SHA-256: %s" % (w.get("sha256") or "unknown"))
        lines.append("Retained workspace (unencrypted intermediate ISO may remain): %s"
                     % (w.get("workspace") or "unknown"))
        if w.get("reason"):
            lines.append("Cleanup failure reason reported by the build: %s"
                         % w["reason"])
        exc = outcome.get("exc")
        if exc is not None:
            lines.append("Build exception: %s: %s" % (type(exc).__name__, exc))
        if outcome.get("log"):
            lines.append("Full diagnostic log: %s" % outcome["log"])
        lines.append("The Personal build is NOT securely complete.")
        text = _bound_detail("\n".join(lines), outcome.get("log"))
        self.main._append_output("")
        self.main._append_output(text)
        self.main.show_message("Personal build not securely complete", text,
                               kind="error")

    def _present_error(self, exc, log_path=None):
        detail = _bound_detail("%s: %s" % (type(exc).__name__, exc), log_path)
        self.main._append_output("")
        self.main._append_output("Personal build failed: " + detail)
        if log_path:
            self.main._append_output("Full diagnostic log: %s" % log_path)
        self.main.show_message("Quail Snap Personal build failed", detail,
                               kind="error")

    def _confirm_sensitive_data(self):
        mw = _mw()
        main = self.main
        dlg = tk.Toplevel(main.root)
        dlg.title(CONFIRM_TITLE)
        dlg.transient(main.root)
        dlg.configure(bg=mw.COLOR_BG)
        dlg.resizable(False, False)
        content = tk.Frame(dlg, bg=mw.COLOR_SURFACE, padx=24, pady=20)
        content.pack(fill="both", expand=True)
        tk.Label(content, text=CONFIRM_TITLE, font=main.font_section,
                 fg=mw.COLOR_TEXT, bg=mw.COLOR_SURFACE, anchor="w").pack(anchor="w")
        tk.Frame(content, height=2, bg=mw.COLOR_PRIMARY_LIGHT).pack(fill="x",
                                                                     pady=(8, 12))
        tk.Label(content, text=CONFIRM_TEXT, font=main.font_label, fg=mw.COLOR_TEXT,
                 bg=mw.COLOR_SURFACE, justify="left", wraplength=560,
                 anchor="w").pack(anchor="w")
        result_box = []

        def on_confirm():
            result_box.append(True)
            dlg.destroy()

        def on_cancel():
            result_box.append(False)
            dlg.destroy()

        btns = tk.Frame(content, bg=mw.COLOR_SURFACE)
        btns.pack(anchor="e", pady=(16, 0))
        main._make_button(btns, "Cancel", on_cancel).pack(side="right", padx=(8, 0))
        main._make_button(btns, "Build Personal ISO", on_confirm).pack(side="right")
        dlg.protocol("WM_DELETE_WINDOW", on_cancel)
        dlg.grab_set()
        dlg.wait_window()
        return bool(result_box and result_box[0])

    def _ask_passphrase(self):
        mw = _mw()
        main = self.main
        dlg = tk.Toplevel(main.root)
        dlg.title("Personal ISO passphrase")
        dlg.transient(main.root)
        dlg.configure(bg=mw.COLOR_BG)
        dlg.resizable(False, False)
        content = tk.Frame(dlg, bg=mw.COLOR_SURFACE, padx=24, pady=20)
        content.pack(fill="both", expand=True)
        tk.Label(content, text="Personal ISO passphrase", font=main.font_section,
                 fg=mw.COLOR_TEXT, bg=mw.COLOR_SURFACE, anchor="w").pack(anchor="w")
        tk.Frame(content, height=2, bg=mw.COLOR_PRIMARY_LIGHT).pack(fill="x",
                                                                     pady=(8, 12))
        form = tk.Frame(content, bg=mw.COLOR_SURFACE)
        form.pack(anchor="w")
        tk.Label(form, text="Passphrase:", font=main.font_label, fg=mw.COLOR_TEXT,
                 bg=mw.COLOR_SURFACE).grid(row=0, column=0, sticky="w", pady=4)
        tk.Label(form, text="Confirm passphrase:", font=main.font_label,
                 fg=mw.COLOR_TEXT, bg=mw.COLOR_SURFACE).grid(row=1, column=0,
                                                              sticky="w", pady=4)
        entry_style = dict(show="*", font=main.font_label, fg=mw.COLOR_TEXT,
                           bg=mw.COLOR_SURFACE, insertbackground=mw.COLOR_TEXT,
                           relief="flat", bd=0, highlightthickness=1,
                           highlightbackground=mw.COLOR_BORDER,
                           highlightcolor=mw.COLOR_PRIMARY)
        e1 = tk.Entry(form, **entry_style)
        e2 = tk.Entry(form, **entry_style)
        e1.grid(row=0, column=1, padx=(8, 0), pady=4)
        e2.grid(row=1, column=1, padx=(8, 0), pady=4)
        err_label = tk.Label(content, text="", font=main.font_label,
                             fg=mw.COLOR_PRIMARY_DARK, bg=mw.COLOR_SURFACE,
                             anchor="w")
        err_label.pack(anchor="w", pady=(8, 0))
        result_box = []

        def on_ok():
            err = _complete_passphrase([e1, e2], result_box)
            if err:
                err_label.configure(text=err)
                return
            dlg.destroy()

        def on_cancel():
            _clear_entries([e1, e2])
            dlg.destroy()

        btns = tk.Frame(content, bg=mw.COLOR_SURFACE)
        btns.pack(anchor="e", pady=(12, 0))
        main._make_button(btns, "Cancel", on_cancel).pack(side="right", padx=(8, 0))
        main._make_button(btns, "OK", on_ok).pack(side="right")
        dlg.protocol("WM_DELETE_WINDOW", on_cancel)
        dlg.grab_set()
        dlg.wait_window()
        if result_box:
            return result_box[0]
        return None