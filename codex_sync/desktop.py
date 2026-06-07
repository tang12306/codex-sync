from __future__ import annotations

import json
import queue
import threading
import tkinter as tk
from tkinter import messagebox, ttk

from .collector import capture_event, create_resume_prompt
from .config import AppConfig, load_config, save_config
from .daemon import run_daemon
from .disaster_backup import create_disaster_backup
from .git_backup import backup_project_to_server, create_patch_snapshot, git_state
from .hooks import hook_status, install_hooks
from .server import flush_outbox, outbox_count, sync_once
from .util import utc_now
from .wsl import pull_wsl_config, wsl_status


class CodexSyncApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Codex Sync Desktop")
        self.geometry("980x680")
        self.minsize(860, 560)
        self.cfg = load_config()
        self.log_queue: queue.Queue[str] = queue.Queue()
        self.daemon_stop = threading.Event()
        self.daemon_thread: threading.Thread | None = None
        self._build()
        self._load_config_to_form()
        self._refresh_status()
        self.after(250, self._drain_logs)
        self.after(500, self.preflight_backup)

    def _build(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(3, weight=1)

        settings = ttk.LabelFrame(self, text="Server and Device")
        settings.grid(row=0, column=0, sticky="ew", padx=10, pady=8)
        for idx in range(6):
            settings.columnconfigure(idx, weight=1 if idx in (1, 3, 5) else 0)

        ttk.Label(settings, text="Server URL").grid(row=0, column=0, sticky="w", padx=6, pady=6)
        self.server_url = ttk.Entry(settings)
        self.server_url.grid(row=0, column=1, sticky="ew", padx=6, pady=6)

        ttk.Label(settings, text="API Token").grid(row=0, column=2, sticky="w", padx=6, pady=6)
        self.api_token = ttk.Entry(settings, show="*")
        self.api_token.grid(row=0, column=3, sticky="ew", padx=6, pady=6)

        ttk.Label(settings, text="Device ID").grid(row=0, column=4, sticky="w", padx=6, pady=6)
        self.device_id = ttk.Entry(settings)
        self.device_id.grid(row=0, column=5, sticky="ew", padx=6, pady=6)

        ttk.Label(settings, text="Interval (s)").grid(row=1, column=0, sticky="w", padx=6, pady=6)
        self.interval = ttk.Entry(settings, width=10)
        self.interval.grid(row=1, column=1, sticky="w", padx=6, pady=6)

        ttk.Label(settings, text="Untracked Max Bytes").grid(row=1, column=2, sticky="w", padx=6, pady=6)
        self.max_untracked = ttk.Entry(settings, width=12)
        self.max_untracked.grid(row=1, column=3, sticky="w", padx=6, pady=6)

        ttk.Label(settings, text="Project Backup").grid(row=1, column=4, sticky="w", padx=6, pady=6)
        ttk.Label(settings, text="patch zip -> sync server").grid(row=1, column=5, sticky="w", padx=6, pady=6)

        actions = ttk.Frame(self)
        actions.grid(row=1, column=0, sticky="ew", padx=10, pady=4)
        for idx in range(8):
            actions.columnconfigure(idx, weight=1)

        buttons = [
            ("Save Config", self.save_config),
            ("Sync Now", lambda: self.run_action("sync-now", lambda: sync_once(load_config()))),
            ("Flush Outbox", lambda: self.run_action("flush-outbox", lambda: flush_outbox(load_config()))),
            ("Install Hooks", lambda: self.run_action("install-hooks", install_hooks)),
            ("Test Capture", self.test_capture),
            ("Git Snapshot", lambda: self.run_action("git-snapshot", lambda: create_patch_snapshot(None, load_config()))),
            ("Project Backup", lambda: self.run_action("project-backup", lambda: backup_project_to_server(None, load_config()))),
            ("Resume Prompt", lambda: self.run_action("resume", lambda: {"path": str(create_resume_prompt(load_config()))})),
        ]
        for idx, (text, command) in enumerate(buttons):
            ttk.Button(actions, text=text, command=command).grid(row=0, column=idx, sticky="ew", padx=3, pady=4)

        actions2 = ttk.Frame(self)
        actions2.grid(row=2, column=0, sticky="ew", padx=10, pady=4)
        for idx in range(7):
            actions2.columnconfigure(idx, weight=1)
        ttk.Button(actions2, text="Preflight Backup", command=lambda: self.preflight_backup(force=True)).grid(row=0, column=0, sticky="ew", padx=3)
        ttk.Button(actions2, text="Start Daemon", command=self.start_daemon).grid(row=0, column=1, sticky="ew", padx=3)
        ttk.Button(actions2, text="Stop Daemon", command=self.stop_daemon).grid(row=0, column=2, sticky="ew", padx=3)
        ttk.Button(actions2, text="Refresh Status", command=self._refresh_status).grid(row=0, column=3, sticky="ew", padx=3)
        ttk.Button(actions2, text="WSL Scan", command=lambda: self.run_action("wsl-status", wsl_status)).grid(row=0, column=4, sticky="ew", padx=3)
        ttk.Button(actions2, text="Pull First WSL Config", command=self.pull_first_wsl).grid(row=0, column=5, sticky="ew", padx=3)
        ttk.Button(actions2, text="Git State", command=lambda: self.run_action("git-state", lambda: git_state())).grid(row=0, column=6, sticky="ew", padx=3)

        main = ttk.PanedWindow(self, orient=tk.HORIZONTAL)
        main.grid(row=3, column=0, sticky="nsew", padx=10, pady=8)

        status_frame = ttk.LabelFrame(main, text="Status")
        self.status = tk.Text(status_frame, height=12, width=34, wrap="word")
        self.status.pack(fill="both", expand=True, padx=6, pady=6)
        self.status.configure(state="disabled")
        main.add(status_frame, weight=1)

        log_frame = ttk.LabelFrame(main, text="Log")
        self.log = tk.Text(log_frame, height=18, wrap="word")
        self.log.pack(fill="both", expand=True, padx=6, pady=6)
        self.log.configure(state="disabled")
        main.add(log_frame, weight=3)

    def _load_config_to_form(self) -> None:
        self.server_url.delete(0, tk.END)
        self.server_url.insert(0, self.cfg.server_url)
        self.api_token.delete(0, tk.END)
        self.api_token.insert(0, self.cfg.api_token)
        self.device_id.delete(0, tk.END)
        self.device_id.insert(0, self.cfg.device_id)
        self.interval.delete(0, tk.END)
        self.interval.insert(0, str(self.cfg.sync_interval_seconds))
        self.max_untracked.delete(0, tk.END)
        self.max_untracked.insert(0, str(self.cfg.max_untracked_copy_bytes))

    def _config_from_form(self) -> AppConfig:
        cfg = load_config()
        cfg.server_url = self.server_url.get().strip()
        cfg.api_token = self.api_token.get().strip()
        cfg.device_id = self.device_id.get().strip() or cfg.device_id
        try:
            cfg.sync_interval_seconds = int(self.interval.get().strip())
        except ValueError:
            cfg.sync_interval_seconds = 180
        try:
            cfg.max_untracked_copy_bytes = int(self.max_untracked.get().strip())
        except ValueError:
            cfg.max_untracked_copy_bytes = 256 * 1024
        return cfg

    def save_config(self) -> None:
        self.cfg = self._config_from_form()
        path = save_config(self.cfg)
        self.write_log(f"Saved config: {path}")
        self._refresh_status()

    def write_log(self, text: str) -> None:
        self.log_queue.put(f"{utc_now()} {text}")

    def _drain_logs(self) -> None:
        while True:
            try:
                line = self.log_queue.get_nowait()
            except queue.Empty:
                break
            self.log.configure(state="normal")
            self.log.insert(tk.END, line + "\n")
            self.log.see(tk.END)
            self.log.configure(state="disabled")
        self.after(250, self._drain_logs)

    def run_action(self, name: str, fn) -> None:  # noqa: ANN001 - tkinter callback wrapper
        def worker() -> None:
            self.write_log(f"Starting {name}")
            try:
                result = fn()
                self.write_log(f"{name} result:\n{json.dumps(result, indent=2, ensure_ascii=False)}")
            except Exception as exc:  # noqa: BLE001 - surface desktop errors
                self.write_log(f"{name} failed: {exc}")
            self.after(0, self._refresh_status)

        threading.Thread(target=worker, daemon=True).start()

    def test_capture(self) -> None:
        self.run_action("test-capture", lambda: capture_event("DesktopTest", '{"message":"desktop capture test"}', load_config()))

    def start_daemon(self) -> None:
        if self.daemon_thread and self.daemon_thread.is_alive():
            self.write_log("Daemon already running")
            return
        self.save_config()
        try:
            protection = create_disaster_backup(load_config(), reason="before_desktop_daemon")
            self.write_log(f"preflight-before-daemon result:\n{json.dumps(protection, indent=2, ensure_ascii=False)}")
        except Exception as exc:  # noqa: BLE001 - surface desktop errors
            self.write_log(f"preflight-before-daemon failed: {exc}")
            messagebox.showerror("Preflight Backup Failed", str(exc))
            return
        self.daemon_stop.clear()
        self.daemon_thread = threading.Thread(
            target=run_daemon,
            args=(load_config(), self.daemon_stop.is_set, self.write_log),
            daemon=True,
        )
        self.daemon_thread.start()
        self.write_log("Daemon started")
        self._refresh_status()

    def preflight_backup(self, force: bool = False) -> None:
        self.run_action("preflight-backup", lambda: create_disaster_backup(load_config(), reason="desktop_start", force=force))

    def stop_daemon(self) -> None:
        self.daemon_stop.set()
        self.write_log("Daemon stop requested")
        self._refresh_status()

    def pull_first_wsl(self) -> None:
        def work() -> dict:
            status = wsl_status()
            distros = status.get("distros", [])
            if not distros:
                return {"error": "No WSL distro found"}
            return pull_wsl_config(distros[0]["distro"])

        self.run_action("wsl-pull-first", work)

    def _refresh_status(self) -> None:
        cfg = load_config()
        hook = hook_status()
        daemon_running = bool(self.daemon_thread and self.daemon_thread.is_alive())
        status = {
            "config_device_id": cfg.device_id,
            "server_configured": bool(cfg.server_url),
            "outbox_count": outbox_count(),
            "daemon_running": daemon_running,
            "hooks": hook,
            "git": git_state(),
        }
        self.status.configure(state="normal")
        self.status.delete("1.0", tk.END)
        self.status.insert(tk.END, json.dumps(status, indent=2, ensure_ascii=False))
        self.status.configure(state="disabled")


def main() -> None:
    app = CodexSyncApp()
    app.mainloop()
