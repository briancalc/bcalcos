import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
import tkinter.font as tkfont

from gui import personal_flow


# BcalcOS light-mode palette and fonts
COLOR_PRIMARY = "#BA7800"
COLOR_PRIMARY_DARK = "#8A5A00"
COLOR_PRIMARY_LIGHT = "#D99A28"
COLOR_PRIMARY_LIGHT_LIGHT = "#EBC978"

COLOR_BG = "#F5F6F8"
COLOR_SURFACE = "#FFFFFF"
COLOR_SURFACE_ALT = "#FBF9F5"

COLOR_TEXT = "#2C3E50"
COLOR_TEXT_MUTED = "#68717A"
COLOR_TEXT_LIGHT = "#9299A0"

COLOR_BORDER = "#E2E5E8"
COLOR_BORDER_GOLD = "#E5C88F"

FONT_PRIMARY = "Inter"
FONT_HEADING = "Poppins"
FONT_MONO = "monospace"


class MainWindow:
    def __init__(self, root):
        self.root = root
        self.root.title("Quail Snap")
        self.root.geometry("780x640")
        self.root.minsize(680, 560)
        self.root.configure(bg=COLOR_BG)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        self._is_root = os.geteuid() == 0
        self._busy = False
        self._task_queue = queue.Queue()
        self._build_start = None
        self._timer_after_id = None

        self.font_title = tkfont.Font(
            family=FONT_HEADING, size=17, weight="bold"
        )
        self.font_section = tkfont.Font(
            family=FONT_HEADING, size=11, weight="bold"
        )
        self.font_label = tkfont.Font(family=FONT_PRIMARY, size=10)
        self.font_button = tkfont.Font(family=FONT_PRIMARY, size=10)
        self.font_output = tkfont.Font(family=FONT_MONO, size=9)

        self._build_interface()
        self.personal_flow = personal_flow.PersonalFlow(self)
        self.personal_button.configure(command=self.personal_flow.start)
        self._update_button_states()

        if not self._is_root:
            self._set_output(
                "Quail Snap requires administrative privileges.\n\n"
                "Close this window and launch the application with sudo.\n\n"
                "No operations are available until it is running as root."
            )
            self.root.after_idle(self._show_root_required)

    # --------------------------------------------------------
    # Interface
    # --------------------------------------------------------

    def _build_interface(self):
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(1, weight=1)

        # Header
        header = tk.Frame(self.root, bg=COLOR_BG)
        header.grid(row=0, column=0, sticky="ew", padx=24, pady=(20, 0))
        header.columnconfigure(0, weight=1)

        tk.Label(
            header, text="Quail Snap", font=self.font_title,
            fg=COLOR_TEXT, bg=COLOR_BG
        ).grid(row=0, column=0, sticky="w")

        self.menu_button = tk.Menubutton(
            header, text="☰", indicatoron=False,
            font=self.font_button, fg=COLOR_TEXT, bg=COLOR_BG,
            activeforeground=COLOR_TEXT,
            activebackground=COLOR_PRIMARY_LIGHT_LIGHT,
            relief="flat", bd=0, highlightthickness=0, cursor="hand2",
            padx=8, pady=2
        )
        self.menu = tk.Menu(
            self.menu_button, tearoff=0,
            bg=COLOR_SURFACE, fg=COLOR_TEXT, font=self.font_label,
            activebackground=COLOR_PRIMARY_LIGHT_LIGHT,
            activeforeground=COLOR_TEXT, bd=0, relief="flat"
        )
        self.menu.add_command(label="About", command=self.show_about)
        self.menu.add_command(label="Close", command=self._on_close)
        self.menu_button["menu"] = self.menu
        self.menu_button.grid(row=0, column=1, sticky="e")

        tk.Label(
            header, text="Create a snapshot of your BcalcOS system",
            font=self.font_label, fg=COLOR_TEXT_MUTED, bg=COLOR_BG
        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(2, 8))

        tk.Frame(
            header, height=2, bg=COLOR_PRIMARY_LIGHT
        ).grid(row=2, column=0, columnspan=2, sticky="ew")

        # Main content
        content = tk.Frame(self.root, bg=COLOR_BG)
        content.grid(
            row=1, column=0, sticky="nsew",
            padx=24, pady=(14, 10)
        )
        content.columnconfigure(0, weight=1)
        content.rowconfigure(1, weight=1)

        # Snapshot mode panel
        mode_panel = tk.Frame(
            content, bg=COLOR_SURFACE,
            highlightbackground=COLOR_BORDER,
            highlightthickness=1, padx=16, pady=14
        )
        mode_panel.grid(row=0, column=0, sticky="ew", pady=(0, 12))
        mode_panel.columnconfigure(0, weight=1)
        mode_panel.columnconfigure(1, weight=1)

        tk.Label(
            mode_panel, text="Snapshot Mode",
            font=self.font_section, fg=COLOR_TEXT,
            bg=COLOR_SURFACE
        ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 8))

        self.distributable_button = self._make_button(
            mode_panel, "Distributable ISO", self.create_iso
        )
        self.distributable_button.grid(
            row=1, column=0, sticky="ew", padx=(0, 5)
        )

        self.personal_button = self._make_button(
            mode_panel, "Personal ISO", None, state="disabled"
        )
        self.personal_button.grid(
            row=1, column=1, sticky="ew", padx=(5, 0)
        )

        # Output panel
        results = tk.Frame(
            content, bg=COLOR_SURFACE,
            highlightbackground=COLOR_BORDER,
            highlightthickness=1
        )
        results.grid(row=1, column=0, sticky="nsew")
        results.columnconfigure(0, weight=1)
        results.rowconfigure(1, weight=1)

        tk.Label(
            results, text="Activity",
            font=self.font_section, fg=COLOR_TEXT,
            bg=COLOR_SURFACE, anchor="w"
        ).grid(
            row=0, column=0, sticky="w",
            padx=14, pady=(10, 6)
        )

        self.build_time_label = tk.Label(
            results, text="Build Time: --:--",
            font=self.font_label, fg=COLOR_TEXT_MUTED,
            bg=COLOR_SURFACE, anchor="e"
        )
        self.build_time_label.grid(
            row=0, column=1, sticky="e",
            padx=(0, 14), pady=(10, 6)
        )

        output_frame = tk.Frame(results, bg=COLOR_SURFACE)
        output_frame.grid(
            row=1, column=0, columnspan=2, sticky="nsew",
            padx=10, pady=(0, 10)
        )
        output_frame.columnconfigure(0, weight=1)
        output_frame.rowconfigure(0, weight=1)

        self.output = tk.Text(
            output_frame, height=12, wrap="word",
            font=self.font_output, fg=COLOR_TEXT,
            bg=COLOR_SURFACE, insertbackground=COLOR_TEXT,
            selectbackground=COLOR_PRIMARY_LIGHT_LIGHT,
            selectforeground=COLOR_TEXT,
            relief="flat", bd=0, padx=8, pady=8,
            state="disabled"
        )
        scrollbar = tk.Scrollbar(
            output_frame, orient="vertical",
            command=self.output.yview
        )
        self.output.configure(yscrollcommand=scrollbar.set)
        self.output.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")

        if self._is_root:
            initial_text = (
                "Create an ISO by selecting a snapshot mode above.\n\n"
                "The build process can take a few minutes."
            )
        else:
            initial_text = "Quail Snap requires administrative privileges."
        self._set_output(initial_text)

    def _make_button(self, parent, text, command, state="normal"):
        normal_bg = COLOR_SURFACE
        hover_bg = COLOR_PRIMARY_LIGHT_LIGHT

        button = tk.Button(
            parent, text=text, command=command, state=state,
            font=self.font_button, fg=COLOR_TEXT,
            bg=normal_bg, activeforeground=COLOR_TEXT,
            activebackground=hover_bg,
            relief="flat", bd=0, highlightthickness=1,
            highlightbackground=COLOR_BORDER,
            highlightcolor=COLOR_PRIMARY,
            padx=10, pady=8, cursor="hand2"
        )
        button.bind(
            "<Enter>",
            lambda _event: button.configure(bg=hover_bg)
            if str(button.cget("state")) == "normal" else None
        )
        button.bind(
            "<Leave>",
            lambda _event: button.configure(bg=normal_bg)
        )
        return button

    def _set_output(self, text):
        self.output.configure(state="normal")
        self.output.delete("1.0", "end")
        self.output.insert("1.0", text)
        self.output.configure(state="disabled")

    def _append_output(self, text):
        self.output.configure(state="normal")
        self.output.insert("end", text + "\n")
        self.output.see("end")
        self.output.configure(state="disabled")

    def _update_button_states(self):
        state = "normal" if self._is_root and not self._busy else "disabled"
        self.distributable_button.configure(state=state)
        self.distributable_button.configure(bg=COLOR_SURFACE)
        self.personal_button.configure(state=state)

    def _show_root_required(self):
        self.show_message(
            "Administrative privileges required",
            "Quail Snap must be launched with sudo.\n\n"
            "Close this window and restart the application with sudo.",
            kind="error",
        )

    # --------------------------------------------------------
    # Background task handling
    # --------------------------------------------------------

    def _poll_task_queue(self):
        while True:
            try:
                status, result, callback = self._task_queue.get_nowait()
            except queue.Empty:
                self.root.after(100, self._poll_task_queue)
                return
            if status == "log":
                self._append_output(result)
                continue
            self._busy = False
            self._update_button_states()
            self._stop_build_timer()
            if status == "success":
                if callback is not None:
                    callback(result)
                else:
                    self._append_output("Done.")
            elif status == "build_error":
                detail = "build_iso.py exited with code %s" % result
                self._append_output("\nBuild failed: " + detail)
                self.show_message(
                    "Quail Snap build failed", detail, kind="error"
                )
            elif status == "error":
                detail = "%s: %s" % (type(result).__name__, result)
                self._set_output("Operation failed.\n\n" + detail)
                self.show_message(
                    "Quail Snap operation failed", detail, kind="error"
                )
            return

    def _on_close(self):
        if self._busy:
            self.show_message(
                "Operation in progress",
                "A Quail Snap operation is still running.\n\n"
                "Please allow it to finish before closing the application.",
                kind="warning",
            )
            return
        if self._timer_after_id is not None:
            self.root.after_cancel(self._timer_after_id)
            self._timer_after_id = None
        self.root.destroy()

    # --------------------------------------------------------
    # Create ISO
    # --------------------------------------------------------

    def create_iso(self):
        if self._busy or not self._is_root:
            return
        self._busy = True
        self._update_button_states()
        self._build_start = time.monotonic()
        self.build_time_label.configure(text="Build Time: 00:00")
        self._timer_after_id = self.root.after(1000, self._tick_build_timer)
        self._set_output(
            "Creating ISO…\n\n"
            "Running the full build pipeline (capture, sanitize, brand, "
            "initramfs, squashfs, boot tree, ISO, verify, export). "
            "This can take a long time."
        )

        def worker():
            exported_path = None
            try:
                env = dict(os.environ)
                env["PYTHONUNBUFFERED"] = "1"
                # build_iso.py resolves the invoking user from SUDO_UID in the
                # inherited environment, so the child env must be a copy of
                # os.environ, never a scrubbed one.
                proc = subprocess.Popen(
                    [sys.executable, "/usr/lib/quail-snapapp/build_iso.py"],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    env=env,
                )
                for line in proc.stdout:
                    line = line.rstrip("\n")
                    if line.startswith("exported:"):
                        exported_path = line.split(":", 1)[1].strip()
                    self._task_queue.put(("log", line, None))
                returncode = proc.wait()
                if returncode == 0:
                    self._task_queue.put(
                        ("success", exported_path, self._on_build_success)
                    )
                else:
                    self._task_queue.put(("build_error", returncode, None))
            except Exception as exc:
                self._task_queue.put(("error", exc, None))

        threading.Thread(target=worker, name="QuailSnapBuild", daemon=True).start()
        self.root.after(100, self._poll_task_queue)

    def _on_build_success(self, exported_path):
        if exported_path:
            self._append_output(
                "\nBuild succeeded.\nExported ISO: %s" % exported_path
            )
        else:
            self._append_output(
                "\nBuild succeeded (exit code 0), but the exported ISO path "
                "could not be determined from the build output."
            )

    @staticmethod
    def _format_elapsed(seconds):
        seconds = int(seconds)
        if seconds < 3600:
            return "%02d:%02d" % (seconds // 60, seconds % 60)
        return "%02d:%02d:%02d" % (seconds // 3600, (seconds % 3600) // 60, seconds % 60)

    def _tick_build_timer(self):
        self._timer_after_id = None
        if self._build_start is None:
            return
        self.build_time_label.configure(
            text="Build Time: "
            + self._format_elapsed(time.monotonic() - self._build_start)
        )
        self._timer_after_id = self.root.after(1000, self._tick_build_timer)

    def _stop_build_timer(self):
        if self._timer_after_id is not None:
            self.root.after_cancel(self._timer_after_id)
            self._timer_after_id = None
        if self._build_start is not None:
            self.build_time_label.configure(
                text="Build Time: "
                + self._format_elapsed(time.monotonic() - self._build_start)
            )
            self._build_start = None

    # --------------------------------------------------------
    # About dialog
    # --------------------------------------------------------

    def show_about(self):
        dialog = tk.Toplevel(self.root)
        dialog.title("About Quail Snap")
        dialog.transient(self.root)
        dialog.configure(bg=COLOR_BG)
        dialog.resizable(False, False)

        def close_dialog():
            dialog.grab_release()
            dialog.destroy()

        dialog.protocol("WM_DELETE_WINDOW", close_dialog)

        content = tk.Frame(dialog, bg=COLOR_SURFACE, padx=24, pady=20)
        content.pack(fill="both", expand=True)
        content.grid_rowconfigure(1, minsize=10)
        content.grid_rowconfigure(3, minsize=10)

        tk.Label(
            content, text="Quail Snap", font=self.font_title,
            fg=COLOR_TEXT, bg=COLOR_SURFACE, anchor="w"
        ).grid(row=0, column=0, sticky="w")

        tk.Label(
            content,
            text="Create a snapshot of your BcalcOS system.",
            font=self.font_label, fg=COLOR_TEXT, bg=COLOR_SURFACE, anchor="w"
        ).grid(row=2, column=0, sticky="w")

        tk.Label(
            content, text="Part of the BcalcOS project.",
            font=self.font_label, fg=COLOR_TEXT_MUTED, bg=COLOR_SURFACE, anchor="w"
        ).grid(row=4, column=0, sticky="w")

        tk.Frame(
            content, height=2, bg=COLOR_PRIMARY_LIGHT
        ).grid(row=5, column=0, sticky="ew", pady=(16, 8))

        self._make_button(content, "Close", close_dialog).grid(
            row=6, column=0, sticky="e"
        )

        dialog.grab_set()

    # --------------------------------------------------------
    # Branded message dialog
    # --------------------------------------------------------

    def show_message(self, title, message, kind="info"):
        accent = {"error": COLOR_PRIMARY_DARK, "warning": COLOR_PRIMARY}.get(
            kind, COLOR_TEXT
        )
        dialog = tk.Toplevel(self.root)
        dialog.title(title)
        dialog.transient(self.root)
        dialog.configure(bg=COLOR_BG)
        dialog.resizable(False, False)

        def close_dialog():
            dialog.grab_release()
            dialog.destroy()

        dialog.protocol("WM_DELETE_WINDOW", close_dialog)

        content = tk.Frame(dialog, bg=COLOR_SURFACE, padx=24, pady=20)
        content.pack(fill="both", expand=True)

        tk.Label(
            content, text=title, font=self.font_section,
            fg=accent, bg=COLOR_SURFACE, anchor="w"
        ).pack(anchor="w")
        tk.Frame(
            content, height=2, bg=COLOR_PRIMARY_LIGHT
        ).pack(fill="x", pady=(8, 12))
        tk.Label(
            content, text=message, font=self.font_label, fg=COLOR_TEXT,
            bg=COLOR_SURFACE, justify="left", wraplength=560, anchor="w"
        ).pack(anchor="w")

        btns = tk.Frame(content, bg=COLOR_SURFACE)
        btns.pack(anchor="e", pady=(16, 0))
        self._make_button(btns, "OK", close_dialog).pack(side="right")

        dialog.grab_set()
        dialog.wait_window()
        return "ok"