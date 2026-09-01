from __future__ import annotations

import ctypes
import logging
import os
import queue
import sys
import threading
import uuid
import webbrowser
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime, timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path
from tkinter import (
    BOTH,
    END,
    LEFT,
    RIGHT,
    BooleanVar,
    StringVar,
    Text,
    Tk,
    Toplevel,
    X,
    filedialog,
    messagebox,
    ttk,
)

import pystray

from . import __version__
from .azure import (
    AzureMetricDefinition,
    AzureResource,
    AzureSubscription,
    AzureWorkspace,
    delete_isolated_azure_state,
    discover_metric_definitions,
    discover_resources,
    discover_workspace_tables,
    discover_workspaces,
    interactive_login,
    list_subscriptions,
    run_check,
    validate_subscription_access,
)
from .branding import apply_window_branding
from .config import (
    AppConfig,
    clear_connection_metadata,
    config_path,
    connection_is_expired,
    export_rule_pack,
    import_rule_pack,
    load_config,
    log_path,
    mark_connection_established,
    parse_resource_reference,
    save_config,
    validate_definition,
)
from .graph_queries import CUSTOM_TEMPLATE, GRAPH_TEMPLATES
from .icons import icon_for
from .model import (
    BeaconState,
    CheckDefinition,
    CheckResult,
    CheckState,
    aggregate_state,
)
from .signal_sources import (
    CUSTOM_LOG_TEMPLATE,
    LOG_TEMPLATES,
    METRIC_OPERATOR_BY_LABEL,
    METRIC_OPERATORS,
    METRIC_REDUCER_BY_LABEL,
    METRIC_REDUCERS,
    PROPERTY_OPERATOR_BY_LABEL,
    PROPERTY_OPERATORS,
    SIGNAL_SOURCES,
    SOURCE_BY_KEY,
    SOURCE_KEY_BY_LABEL,
    VM_POWER_STATES,
)
from .ui_theme import apply_theme
from .updater import (
    ReleaseInfo,
    download_verified_installer,
    fetch_latest_release,
    is_newer_version,
    launch_installer,
)
from .windows_startup import set_startup_enabled

LOGGER = logging.getLogger(__name__)
UPDATE_CHECK_INTERVAL = timedelta(hours=24)
PROJECT_URL = "https://github.com/Anders0lesen/azure-extensible-systray-monitor"

STATE_LABELS = {
    BeaconState.HEALTHY: "Everything is healthy",
    BeaconState.UNCONNECTABLE: "Unable to determine status",
    BeaconState.CONNECTING: "Connecting to Azure",
    BeaconState.FAILED: "Action required",
    BeaconState.CHECKING: "Checking Azure",
}


def _acquire_single_instance() -> object | None:
    if os.name != "nt":
        return object()
    handle = ctypes.windll.kernel32.CreateMutexW(
        None, False, "Local\\AzureHealthBeacon.SingleInstance"
    )
    if ctypes.windll.kernel32.GetLastError() == 183:
        return None
    return handle


class BeaconApp:
    def __init__(self, *, startup_launch: bool = False) -> None:
        self.startup_launch = startup_launch
        self.root = Tk()
        apply_window_branding(self.root)
        self.root.withdraw()
        self.root.title("Azure Health Beacon")
        self.root.protocol("WM_DELETE_WINDOW", self.hide_status)
        self.config = self._load_config_safely()
        self.theme_buttons: list[ttk.Button] = []
        apply_theme(self.root, self.config.theme_mode)
        self.connection_expired_on_start = False
        self.connection_purge_error = ""
        if self.config.connection_purge_pending or connection_is_expired(self.config):
            self.config.connection_purge_pending = True
            clear_connection_metadata(self.config)
            save_config(self.config)
            try:
                delete_isolated_azure_state()
            except OSError as error:
                LOGGER.exception("Could not delete expired isolated Azure state")
                self.connection_purge_error = str(error)
            else:
                self.config.connection_purge_pending = False
                save_config(self.config)
            self.connection_expired_on_start = True
        self.initialized_event = threading.Event()
        if self.config.onboarding_completed:
            self.initialized_event.set()
        self.results: dict[str, CheckResult] = {}
        self.state = BeaconState.CONNECTING
        self.previous_stable_state: BeaconState | None = None
        self.status_window: Toplevel | None = None
        self.setup_window: SetupWizard | None = None
        self.settings_window: Toplevel | None = None
        self.update_status_text = StringVar(value="Updates have not been checked yet.")
        self.available_update: ReleaseInfo | None = None
        self.update_in_progress = False
        self.status_body: ttk.Frame | None = None
        self.stop_event = threading.Event()
        self.check_now_event = threading.Event()
        self.ui_events: queue.Queue[tuple[str, object]] = queue.Queue()
        self.frame = 0
        self.icon = pystray.Icon(
            "AzureHealthBeacon",
            icon_for(self.state),
            "Azure Health Beacon — Connecting",
            menu=pystray.Menu(
                pystray.MenuItem(
                    "Open Azure Health Beacon", self._menu_show, default=True
                ),
                pystray.MenuItem("Azure connection setup", self._menu_setup),
                pystray.MenuItem(
                    "Check now",
                    self._menu_check,
                    enabled=lambda _item: self.config.onboarding_completed,
                ),
                pystray.MenuItem(
                    "Manage checks",
                    self._menu_manage,
                    enabled=lambda _item: self.config.onboarding_completed,
                ),
                pystray.MenuItem("Check for updates…", self._menu_check_updates),
                pystray.MenuItem("Settings…", self._menu_update_settings),
                pystray.MenuItem("About…", self._menu_about),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem(
                    "Delete Azure connection…",
                    self._menu_delete_connection,
                    enabled=lambda _item: self.config.onboarding_completed,
                ),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Exit", self._menu_exit),
            ),
        )

    def _load_config_safely(self) -> AppConfig:
        try:
            return load_config()
        except Exception as error:
            LOGGER.exception("Could not load configuration")
            messagebox.showerror(
                "Azure Health Beacon", f"Could not load configuration:\n\n{error}"
            )
            return AppConfig()

    def run(self) -> None:
        self.icon.run_detached()
        threading.Thread(
            target=self._monitor_loop, name="beacon-monitor", daemon=True
        ).start()
        if not self.config.onboarding_completed:
            self._set_state(BeaconState.UNCONNECTABLE)
            self.icon.title = "Azure Health Beacon — Setup required"
            self.root.after(250, self.show_setup)
        elif not self.config.start_minimized:
            self.root.after(250, self.show_status)
        self.root.after(100, self._process_ui_events)
        self.root.after(125, self._animate)
        self.root.after(3000, self._scheduled_update_check)
        self.root.mainloop()

    def _monitor_loop(self) -> None:
        first_run = True
        while not self.stop_event.is_set():
            if not self.config.onboarding_completed:
                self.initialized_event.wait(1)
                continue
            if connection_is_expired(self.config):
                self.ui_events.put(("connection_expired", None))
                self.stop_event.wait(1)
                continue
            if first_run:
                self.ui_events.put(("state", BeaconState.CONNECTING))
                first_run = False
            self._run_all_checks()
            wait_seconds = max(60, self.config.interval_minutes * 60)
            self.check_now_event.wait(wait_seconds)
            self.check_now_event.clear()

    def _run_all_checks(self) -> None:
        enabled = [check for check in self.config.checks if check.enabled]
        current = list(self.results.values())
        self.ui_events.put(("state", aggregate_state(current, checking=True)))
        if not enabled:
            self.ui_events.put(("results", {}))
            return
        new_results: dict[str, CheckResult] = {}
        with ThreadPoolExecutor(max_workers=min(4, len(enabled))) as executor:
            futures = {
                executor.submit(
                    run_check,
                    definition,
                    timeout_seconds=self.config.timeout_seconds,
                    retry_count=self.config.retry_count,
                ): definition
                for definition in enabled
            }
            for future in as_completed(futures):
                definition = futures[future]
                try:
                    result = future.result()
                except Exception as error:
                    LOGGER.exception(
                        "Unexpected checker failure for %s", definition.name
                    )
                    result = CheckResult(
                        definition.id,
                        definition.name,
                        CheckState.UNCONNECTABLE,
                        f"Unexpected checker error: {error}",
                        portal_url=definition.portal_url,
                    )
                previous = self.results.get(result.check_id)
                if result.state is CheckState.FAILED:
                    if previous and previous.state is CheckState.FAILED:
                        result.first_detected_at = (
                            previous.first_detected_at or previous.checked_at
                        )
                    else:
                        result.first_detected_at = result.checked_at
                new_results[result.check_id] = result
        self.ui_events.put(("results", new_results))

    def _process_ui_events(self) -> None:
        try:
            while True:
                event, payload = self.ui_events.get_nowait()
                if event == "state":
                    self._set_state(payload)  # type: ignore[arg-type]
                elif event == "results":
                    if not self.config.onboarding_completed:
                        continue
                    self.results = payload  # type: ignore[assignment]
                    self._set_state(aggregate_state(list(self.results.values())))
                    self._refresh_status()
                elif event == "show":
                    self.show_status()
                elif event == "manage":
                    self.show_manager()
                elif event == "setup":
                    self.show_setup()
                elif event == "delete_connection":
                    self.delete_connection()
                elif event == "connection_expired":
                    self.expire_connection()
                elif event == "check_updates":
                    self.show_settings()
                    self.check_for_updates(interactive=True)
                elif event == "update_settings":
                    self.show_settings()
                elif event == "about":
                    self.show_about()
                elif event == "update_checked":
                    release, interactive = payload  # type: ignore[misc]
                    self._handle_update_checked(release, interactive)
                elif event == "update_error":
                    error, interactive = payload  # type: ignore[misc]
                    self._handle_update_error(str(error), interactive)
                elif event == "update_downloaded":
                    installer, automatic = payload  # type: ignore[misc]
                    self._handle_update_downloaded(installer, automatic)
                elif event == "exit":
                    self.shutdown()
        except queue.Empty:
            pass
        if not self.stop_event.is_set():
            self.root.after(100, self._process_ui_events)

    def _set_state(self, state: BeaconState) -> None:
        previous = self.state
        self.state = state
        self.icon.title = f"Azure Health Beacon — {STATE_LABELS[state]}"
        self.icon.icon = icon_for(state, self.frame)
        if state in {
            BeaconState.HEALTHY,
            BeaconState.FAILED,
            BeaconState.UNCONNECTABLE,
        }:
            if previous is not state:
                if state is BeaconState.FAILED:
                    failed_names = [
                        r.name
                        for r in self.results.values()
                        if r.state is CheckState.FAILED
                    ]
                    body = ", ".join(failed_names[:3]) or "An Azure check failed."
                    self.icon.notify(body, "Azure Health Beacon — Action required")
                elif (
                    state is BeaconState.HEALTHY
                    and self.previous_stable_state is BeaconState.FAILED
                ):
                    self.icon.notify(
                        "All configured Azure checks are healthy again.",
                        "Azure Health Beacon — Recovered",
                    )
            self.previous_stable_state = state

    def _animate(self) -> None:
        self.frame = (self.frame + 1) % 16
        if self.state in {BeaconState.CONNECTING, BeaconState.CHECKING}:
            self.icon.icon = icon_for(self.state, self.frame)
        if not self.stop_event.is_set():
            self.root.after(125, self._animate)

    def _menu_show(self, _icon: pystray.Icon, _item: pystray.MenuItem) -> None:
        self.ui_events.put(("show", None))

    def _menu_check(self, _icon: pystray.Icon, _item: pystray.MenuItem) -> None:
        if self.config.onboarding_completed:
            self.check_now_event.set()

    def _menu_setup(self, _icon: pystray.Icon, _item: pystray.MenuItem) -> None:
        self.ui_events.put(("setup", None))

    def _menu_delete_connection(
        self, _icon: pystray.Icon, _item: pystray.MenuItem
    ) -> None:
        self.ui_events.put(("delete_connection", None))

    def _menu_manage(self, _icon: pystray.Icon, _item: pystray.MenuItem) -> None:
        self.ui_events.put(("manage", None))

    def _menu_check_updates(self, _icon: pystray.Icon, _item: pystray.MenuItem) -> None:
        self.ui_events.put(("check_updates", None))

    def _menu_update_settings(
        self, _icon: pystray.Icon, _item: pystray.MenuItem
    ) -> None:
        self.ui_events.put(("update_settings", None))

    def _menu_about(self, _icon: pystray.Icon, _item: pystray.MenuItem) -> None:
        self.ui_events.put(("about", None))

    def _menu_exit(self, _icon: pystray.Icon, _item: pystray.MenuItem) -> None:
        self.ui_events.put(("exit", None))

    def theme_button(self, parent: ttk.Frame) -> ttk.Button:
        label = "☀  Light" if self.config.theme_mode == "dark" else "🌙  Dark"
        button = ttk.Button(
            parent,
            text=label,
            style="Theme.TButton",
            command=self.toggle_theme,
        )
        self.theme_buttons.append(button)
        return button

    def toggle_theme(self) -> None:
        self.config.theme_mode = "light" if self.config.theme_mode == "dark" else "dark"
        save_config(self.config)
        apply_theme(self.root, self.config.theme_mode)
        label = "☀  Light" if self.config.theme_mode == "dark" else "🌙  Dark"
        live_buttons = []
        for button in self.theme_buttons:
            try:
                if button.winfo_exists():
                    button.configure(text=label)
                    live_buttons.append(button)
            except Exception:
                continue
        self.theme_buttons = live_buttons

    def show_status(self) -> None:
        if not self.config.onboarding_completed:
            self.show_setup()
            return
        if self.status_window and self.status_window.winfo_exists():
            self.status_window.deiconify()
            self.status_window.lift()
            self.status_window.focus_force()
            self._refresh_status()
            return
        window = Toplevel(self.root)
        apply_window_branding(window)
        self.status_window = window
        window.title("Azure Health Beacon")
        window.geometry("560x420")
        window.minsize(500, 320)
        window.protocol("WM_DELETE_WINDOW", self.hide_status)
        header = ttk.Frame(window, padding=16)
        header.pack(fill=X)
        self.theme_button(header).pack(side=RIGHT)
        ttk.Label(header, textvariable=self._state_text(), style="Title.TLabel").pack(
            anchor="w"
        )
        ttk.Label(header, text="Click a failed item to open it in Azure Portal.").pack(
            anchor="w", pady=(4, 0)
        )
        container = ttk.Frame(window, padding=(16, 0, 16, 8))
        container.pack(fill=BOTH, expand=True)
        self.status_body = container
        footer = ttk.Frame(window, padding=16)
        footer.pack(fill=X)
        ttk.Button(footer, text="Manage checks", command=self.show_manager).pack(
            side=LEFT
        )
        ttk.Button(footer, text="Settings", command=self.show_settings).pack(
            side=LEFT, padx=(8, 0)
        )
        ttk.Button(footer, text="Check now", command=self.check_now_event.set).pack(
            side=RIGHT
        )
        self._refresh_status()
        apply_theme(self.root, self.config.theme_mode)

    def _state_text(self) -> StringVar:
        value = StringVar(value=STATE_LABELS[self.state])

        def update() -> None:
            if value._root().winfo_exists():
                value.set(STATE_LABELS[self.state])
                self.root.after(250, update)

        self.root.after(250, update)
        return value

    def hide_status(self) -> None:
        if self.status_window and self.status_window.winfo_exists():
            self.status_window.withdraw()

    def _refresh_status(self) -> None:
        if not self.status_body or not self.status_body.winfo_exists():
            return
        for child in self.status_body.winfo_children():
            child.destroy()
        if not self.config.checks:
            ttk.Label(
                self.status_body,
                text="No checks configured yet. Add the Orion firewall to begin.",
                wraplength=480,
            ).pack(anchor="w", pady=16)
            ttk.Button(
                self.status_body, text="Add first check", command=self.show_manager
            ).pack(anchor="w")
            return
        order = {
            CheckState.FAILED: 0,
            CheckState.UNCONNECTABLE: 1,
            CheckState.HEALTHY: 2,
        }
        results = sorted(
            self.results.values(),
            key=lambda item: (order[item.state], item.name.casefold()),
        )
        for result in results:
            card = ttk.LabelFrame(self.status_body, text=result.name, padding=10)
            card.pack(fill=X, pady=(0, 8))
            state_text = {
                CheckState.FAILED: "FAILED",
                CheckState.UNCONNECTABLE: "UNCONNECTABLE",
                CheckState.HEALTHY: "HEALTHY",
            }[result.state]
            state_style = {
                CheckState.FAILED: "Failed.TLabel",
                CheckState.UNCONNECTABLE: "Unknown.TLabel",
                CheckState.HEALTHY: "Healthy.TLabel",
            }[result.state]
            ttk.Label(card, text=state_text, style=state_style).pack(anchor="w")
            ttk.Label(card, text=result.summary, wraplength=470).pack(
                anchor="w", pady=(3, 0)
            )
            for finding in result.findings[:5]:
                detail = f"• {finding.title}"
                if finding.summary:
                    detail += f" — {finding.summary}"
                ttk.Label(card, text=detail, wraplength=470).pack(
                    anchor="w", pady=(2, 0)
                )
            timing = f"Last checked: {result.checked_at.strftime('%d %b %Y, %H:%M:%S')}"
            if result.first_detected_at:
                timing += f"  •  First detected: {result.first_detected_at.strftime('%H:%M:%S')}"
            ttk.Label(card, text=timing).pack(anchor="w", pady=(3, 0))
            if result.portal_url:
                ttk.Button(
                    card,
                    text="Open in Azure Portal",
                    command=lambda url=result.portal_url: webbrowser.open(url),
                ).pack(anchor="e", pady=(6, 0))

    def show_manager(self) -> None:
        if not self.config.onboarding_completed:
            self.show_setup()
            return
        CheckManager(self)

    def show_setup(self) -> None:
        if self.setup_window and self.setup_window.window.winfo_exists():
            self.setup_window.window.deiconify()
            self.setup_window.window.lift()
            self.setup_window.window.focus_force()
            return
        self.setup_window = SetupWizard(self)

    def complete_setup(self, subscription: AzureSubscription) -> None:
        if self.config.connection_purge_pending:
            raise RuntimeError(
                "The previous Azure profile has not been deleted; retry setup first"
            )
        self.config.onboarding_completed = True
        self.config.azure_subscription_id = subscription.id
        self.config.azure_subscription_name = subscription.name
        self.config.azure_tenant_id = subscription.tenant_id
        mark_connection_established(self.config)
        save_config(self.config)
        self.connection_expired_on_start = False
        self.initialized_event.set()
        self._set_state(BeaconState.CONNECTING)
        self.icon.update_menu()
        self.root.after(150, self.show_manager)

    def _clear_connection(self) -> None:
        self.config.connection_purge_pending = True
        clear_connection_metadata(self.config)
        save_config(self.config)
        self.initialized_event.clear()
        self.results = {}
        self._set_state(BeaconState.UNCONNECTABLE)
        self.icon.title = "Azure Health Beacon — Setup required"
        self.icon.update_menu()
        self._refresh_status()
        delete_isolated_azure_state()
        self.config.connection_purge_pending = False
        save_config(self.config)

    def prepare_for_login(self) -> tuple[bool, str]:
        if not self.config.connection_purge_pending:
            return True, ""
        try:
            delete_isolated_azure_state()
        except OSError as error:
            self.connection_purge_error = str(error)
            return (
                False,
                "The previous isolated Azure profile could not be deleted. Close Azure activity and retry.",
            )
        self.config.connection_purge_pending = False
        self.connection_purge_error = ""
        save_config(self.config)
        return True, ""

    def delete_connection(self) -> None:
        confirmed = messagebox.askyesno(
            "Delete Azure connection?",
            "This permanently deletes Azure Health Beacon's app-owned DPAPI-encrypted OAuth cache and saved "
            "connection binding. Monitoring stops, but rules are retained. Your Windows work account is not changed.",
            parent=self.status_window
            if self.status_window and self.status_window.winfo_exists()
            else self.root,
        )
        if not confirmed:
            return
        try:
            self._clear_connection()
        except OSError as error:
            messagebox.showerror(
                "Could not delete Azure connection", str(error), parent=self.root
            )
            return
        self.show_setup()

    def expire_connection(self) -> None:
        if not self.config.onboarding_completed:
            return
        try:
            self._clear_connection()
        except OSError:
            LOGGER.exception("Could not purge the expired Azure connection")
            self.connection_expired_on_start = True
            self.show_setup()
            return
        self.connection_expired_on_start = True
        self.icon.notify(
            "The 14-day authorization lease ended. Sign in and validate Azure access again.",
            "Azure Health Beacon — Connection expired",
        )
        self.show_setup()

    def reload_config(self) -> None:
        self.config = self._load_config_safely()
        self.results = {
            key: value
            for key, value in self.results.items()
            if any(c.id == key for c in self.config.checks)
        }
        self._refresh_status()
        self.check_now_event.set()

    def _scheduled_update_check(self) -> None:
        if self.stop_event.is_set():
            return
        if self.config.update_mode != "manual" and not self.update_in_progress:
            due = True
            if self.config.last_update_check_utc:
                try:
                    last = datetime.fromisoformat(self.config.last_update_check_utc)
                    if last.tzinfo is None:
                        last = last.replace(tzinfo=UTC)
                    due = (
                        datetime.now(UTC) - last.astimezone(UTC)
                        >= UPDATE_CHECK_INTERVAL
                    )
                except ValueError:
                    due = True
            if due:
                self.check_for_updates(interactive=False)
        self.root.after(60 * 60 * 1000, self._scheduled_update_check)

    def check_for_updates(self, *, interactive: bool) -> None:
        if self.update_in_progress:
            if interactive:
                messagebox.showinfo(
                    "Azure Health Beacon",
                    "An update check or download is already running.",
                    parent=self.root,
                )
            return
        self.update_in_progress = True

        def worker() -> None:
            try:
                release = fetch_latest_release()
            except Exception as error:
                LOGGER.exception("Update check failed")
                self.ui_events.put(("update_error", (error, interactive)))
            else:
                self.ui_events.put(("update_checked", (release, interactive)))

        threading.Thread(target=worker, name="beacon-update-check", daemon=True).start()

    def _handle_update_checked(self, release: ReleaseInfo, interactive: bool) -> None:
        self.update_in_progress = False
        self.config.last_update_check_utc = datetime.now(UTC).isoformat()
        save_config(self.config)
        if not is_newer_version(release.version):
            self.update_status_text.set(
                "✅ Fully up to date — no new updates available"
            )
            return
        self.update_status_text.set(f"Version {release.version} is available.")
        self.available_update = release
        self.icon.update_menu()
        if self.config.update_mode == "automatic" and not interactive:
            self._download_update(release, automatic=True)
            return
        if self.config.update_mode == "notify" and not interactive:
            self.icon.notify(
                f"Version {release.version} is ready. Open Update settings to install it.",
                "Azure Health Beacon — Update available",
            )
            return
        install = messagebox.askyesno(
            "Azure Health Beacon update",
            f"Version {release.version} is available.\n\nDownload, verify, and install it now? "
            "The Beacon will close and restart after installation.",
            parent=self.root,
        )
        if install:
            self._download_update(release, automatic=False)

    def _handle_update_error(self, error: str, interactive: bool) -> None:
        self.update_in_progress = False
        self.update_status_text.set("Could not check for updates.")
        if interactive:
            messagebox.showerror(
                "Could not check for updates",
                f"No update was installed.\n\n{error}",
                parent=self.root,
            )

    def _download_update(self, release: ReleaseInfo, *, automatic: bool) -> None:
        if self.update_in_progress:
            return
        self.update_in_progress = True
        if not automatic:
            self.icon.notify(
                "Downloading and verifying the installer.",
                "Azure Health Beacon — Updating",
            )

        def worker() -> None:
            try:
                installer = download_verified_installer(release)
            except Exception as error:
                LOGGER.exception("Update download failed")
                self.ui_events.put(("update_error", (error, not automatic)))
            else:
                self.ui_events.put(("update_downloaded", (installer, automatic)))

        threading.Thread(
            target=worker, name="beacon-update-download", daemon=True
        ).start()

    def _handle_update_downloaded(self, installer: Path, automatic: bool) -> None:
        try:
            launch_installer(installer, automatic=automatic)
        except OSError as error:
            self._handle_update_error(str(error), not automatic)
            return
        self.shutdown()

    def show_update_settings(self) -> None:
        self.show_settings()

    def show_settings(self) -> None:
        if self.settings_window and self.settings_window.winfo_exists():
            self.settings_window.deiconify()
            self.settings_window.lift()
            self.settings_window.focus_force()
            return
        window = Toplevel(self.root)
        apply_window_branding(window)
        self.settings_window = window
        window.title("Azure Health Beacon settings")
        window.geometry("610x570")
        window.resizable(False, False)
        window.protocol("WM_DELETE_WINDOW", window.destroy)
        body = ttk.Frame(window, padding=24)
        body.pack(fill=BOTH, expand=True)
        theme = self.theme_button(body)
        theme.pack(anchor="ne")
        ttk.Label(
            body,
            text="Settings",
            style="Title.TLabel",
        ).pack(anchor="w")
        ttk.Label(body, text="Windows", font=("Segoe UI", 11, "bold")).pack(
            anchor="w", pady=(18, 4)
        )
        start_with_windows = BooleanVar(value=self.config.start_with_windows)
        start_minimized = BooleanVar(value=self.config.start_minimized)
        ttk.Checkbutton(
            body,
            text="Start Azure Health Beacon when I sign in to Windows",
            variable=start_with_windows,
        ).pack(anchor="w")
        ttk.Checkbutton(
            body,
            text="Start minimized in the notification area",
            variable=start_minimized,
        ).pack(anchor="w", pady=(4, 0))
        ttk.Label(
            body,
            text="Both options are off until you explicitly enable them.",
        ).pack(anchor="w", padx=(24, 0), pady=(2, 10))
        ttk.Separator(body).pack(fill=X, pady=(4, 8))
        ttk.Label(
            body,
            text=f"Updates — current version {__version__}",
            font=("Segoe UI", 11, "bold"),
        ).pack(anchor="w")
        ttk.Label(
            body,
            text="Automatic updating is off unless you explicitly select it here.",
            wraplength=510,
        ).pack(anchor="w", pady=(6, 16))
        selected = StringVar(value=self.config.update_mode)
        choices = (
            (
                "manual",
                "Manual only (default)",
                "The Beacon makes no background update requests. Use Check now when you choose.",
            ),
            (
                "notify",
                "Notify me",
                "Check GitHub once per day and tell me when an update is available.",
            ),
            (
                "automatic",
                "Install automatically",
                "Check once per day, verify the SHA-256 checksum, install silently, and restart the Beacon.",
            ),
        )
        for value, title, description in choices:
            ttk.Radiobutton(body, text=title, variable=selected, value=value).pack(
                anchor="w", pady=(4, 0)
            )
            ttk.Label(body, text=description, wraplength=480).pack(
                anchor="w", padx=(24, 0)
            )

        buttons = ttk.Frame(body)
        buttons.pack(fill=X, side="bottom", pady=(18, 0))

        def save() -> None:
            choice = selected.get()
            if choice == "automatic" and self.config.update_mode != "automatic":
                confirmed = messagebox.askyesno(
                    "Enable automatic installation?",
                    "The Beacon will be allowed to download verified installers from this project's GitHub "
                    "releases, install them silently for your Windows account, close, and restart itself.\n\n"
                    "Enable this opt-in setting?",
                    parent=window,
                )
                if not confirmed:
                    return
            self.config.update_mode = choice
            try:
                set_startup_enabled(start_with_windows.get())
            except OSError as error:
                messagebox.showerror(
                    "Could not change Windows startup",
                    str(error),
                    parent=window,
                )
                return
            self.config.start_with_windows = start_with_windows.get()
            self.config.start_minimized = start_minimized.get()
            save_config(self.config)
            window.destroy()
            if choice != "manual":
                self.config.last_update_check_utc = ""
                save_config(self.config)
                self.check_for_updates(interactive=False)

        ttk.Button(
            buttons,
            text="Check for updates",
            command=lambda: (
                self.update_status_text.set("Checking GitHub releases…"),
                self.check_for_updates(interactive=True),
            ),
        ).pack(side=LEFT)
        ttk.Label(body, textvariable=self.update_status_text, wraplength=530).pack(
            anchor="w", side="bottom", pady=(8, 0)
        )
        ttk.Button(buttons, text="Save", command=save).pack(side=RIGHT)
        ttk.Button(buttons, text="Cancel", command=window.destroy).pack(
            side=RIGHT, padx=(0, 8)
        )
        ttk.Button(buttons, text="About", command=self.show_about).pack(
            side=RIGHT, padx=(0, 8)
        )
        apply_theme(self.root, self.config.theme_mode)

    def show_about(self) -> None:
        window = Toplevel(self.root)
        apply_window_branding(window)
        window.title("About Azure Health Beacon")
        window.geometry("520x350")
        window.resizable(False, False)
        body = ttk.Frame(window, padding=28)
        body.pack(fill=BOTH, expand=True)
        self.theme_button(body).pack(anchor="ne")
        ttk.Label(body, text="Azure Health Beacon", style="Title.TLabel").pack(
            anchor="w"
        )
        ttk.Label(body, text=f"Version {__version__}", style="Muted.TLabel").pack(
            anchor="w", pady=(2, 18)
        )
        ttk.Label(
            body,
            text=(
                "A read-only Windows 11 tray monitor for user-defined Azure signals. "
                "Authentication stays inside Microsoft's sign-in flow and the Beacon's app-owned encrypted cache."
            ),
            wraplength=450,
        ).pack(anchor="w")
        ttk.Label(body, text="Project and source code", style="Section.TLabel").pack(
            anchor="w", pady=(24, 5)
        )
        ttk.Button(
            body,
            text="Open GitHub repository ↗",
            command=lambda: webbrowser.open(PROJECT_URL),
        ).pack(anchor="w")
        ttk.Label(body, text=PROJECT_URL, style="Muted.TLabel").pack(
            anchor="w", pady=(5, 0)
        )
        ttk.Button(body, text="Close", command=window.destroy).pack(
            anchor="e", side="bottom"
        )
        apply_theme(self.root, self.config.theme_mode)

    def shutdown(self) -> None:
        self.stop_event.set()
        self.check_now_event.set()
        try:
            self.icon.stop()
        finally:
            self.root.quit()


class SetupWizard:
    def __init__(self, app: BeaconApp) -> None:
        self.app = app
        self.window = Toplevel(app.root)
        apply_window_branding(self.window)
        self.window.title("Set up Azure Health Beacon")
        self.window.geometry("620x470")
        self.window.minsize(560, 420)
        self.window.protocol("WM_DELETE_WINDOW", self._close)
        self.window.lift()
        self.window.focus_force()
        self.tenant_hint = StringVar(value=app.config.azure_tenant_id)
        initial_message = (
            "The previous Azure profile is pending secure deletion. Close Azure activity, then retry sign-in."
            if app.config.connection_purge_pending
            else "The previous 14-day connection lease expired and was deleted. Sign in again to continue."
            if app.connection_expired_on_start
            else "Sign in to create this app's isolated Azure connection."
        )
        self.status = StringVar(value=initial_message)
        self.subscriptions: list[AzureSubscription] = []
        self.selected_subscription: AzureSubscription | None = None
        self.page = ttk.Frame(self.window, padding=24)
        self.page.pack(fill=BOTH, expand=True)
        self._build_sign_in_page()
        apply_theme(app.root, app.config.theme_mode)

    def _clear_page(self) -> None:
        for child in self.page.winfo_children():
            child.destroy()

    def _build_sign_in_page(self) -> None:
        self._clear_page()
        self.app.theme_button(self.page).pack(anchor="ne")
        ttk.Label(
            self.page, text="Connect Azure Health Beacon", style="Title.TLabel"
        ).pack(anchor="w")
        ttk.Label(
            self.page,
            text=(
                "Before any monitoring can start, connect a Microsoft Azure account and verify that it can read "
                "the intended subscription."
            ),
            wraplength=540,
        ).pack(anchor="w", pady=(8, 18))
        security = ttk.LabelFrame(self.page, text="Credential safety", padding=12)
        security.pack(fill=X)
        ttk.Label(
            security,
            text=(
                "Your username, password, and MFA response stay inside Microsoft's sign-in flow. OAuth tokens are "
                "persisted only as Windows DPAPI ciphertext, separate from rules and configuration. The complete "
                "app-owned identity cache is deleted after 14 days."
            ),
            wraplength=510,
        ).pack(anchor="w")
        ttk.Label(
            self.page, text="Tenant ID (optional; useful for multi-tenant accounts)"
        ).pack(anchor="w", pady=(18, 3))
        ttk.Entry(self.page, textvariable=self.tenant_hint).pack(fill=X)
        ttk.Label(self.page, textvariable=self.status, wraplength=540).pack(
            anchor="w", pady=(16, 10)
        )
        actions = ttk.Frame(self.page)
        actions.pack(fill=X, pady=(6, 0))
        self.login_button = ttk.Button(
            actions, text="Sign in with Microsoft", command=self._sign_in
        )
        self.login_button.pack(side=RIGHT)
        apply_theme(self.app.root, self.app.config.theme_mode)

    def _set_sign_in_busy(self, busy: bool) -> None:
        state = ["disabled"] if busy else ["!disabled"]
        self.login_button.state(state)

    def _sign_in(self) -> None:
        ready, error = self.app.prepare_for_login()
        if not ready:
            self.status.set(error)
            return
        self._set_sign_in_busy(True)
        self.status.set(
            "Opening Microsoft's secure sign-in… Complete the prompt that appears."
        )

        def worker() -> None:
            success, message = interactive_login(self.tenant_hint.get())
            if not success:
                self.window.after(0, lambda: self._sign_in_failed(message))
                return
            subscriptions, error = list_subscriptions()
            if error:
                self.window.after(0, lambda: self._sign_in_failed(error))
                return
            self.window.after(0, lambda: self._build_subscription_page(subscriptions))

        threading.Thread(
            target=worker, name="azure-interactive-login", daemon=True
        ).start()

    def _sign_in_failed(self, message: str) -> None:
        self.status.set(message)
        self._set_sign_in_busy(False)

    def _build_subscription_page(self, subscriptions: list[AzureSubscription]) -> None:
        self.subscriptions = subscriptions
        self._clear_page()
        self.app.theme_button(self.page).pack(anchor="ne")
        ttk.Label(self.page, text="Choose the Azure scope", style="Title.TLabel").pack(
            anchor="w"
        )
        ttk.Label(
            self.page,
            text="Select the subscription the Beacon should validate. The Beacon has no global Azure CLI context.",
            wraplength=540,
        ).pack(anchor="w", pady=(8, 18))
        labels = [f"{item.name} — {item.id}" for item in subscriptions]
        self.subscription_choice = ttk.Combobox(
            self.page, values=labels, state="readonly"
        )
        self.subscription_choice.pack(fill=X)
        selected_index = 0
        for index, item in enumerate(subscriptions):
            if item.id.casefold() == self.app.config.azure_subscription_id.casefold():
                selected_index = index
                break
        self.subscription_choice.current(selected_index)
        self.validation_status = StringVar(
            value="Run a live, read-only Azure request before continuing."
        )
        ttk.Label(self.page, textvariable=self.validation_status, wraplength=540).pack(
            anchor="w", pady=(18, 10)
        )
        actions = ttk.Frame(self.page)
        actions.pack(fill=X, pady=(8, 0))
        ttk.Button(actions, text="Back", command=self._build_sign_in_page).pack(
            side=LEFT
        )
        self.validate_button = ttk.Button(
            actions, text="Verify Azure access", command=self._validate
        )
        self.validate_button.pack(side=RIGHT)
        self.finish_button = ttk.Button(
            self.page, text="Continue to checks", command=self._finish
        )
        self.finish_button.pack(anchor="e", pady=(28, 0))
        self.finish_button.state(["disabled"])
        apply_theme(self.app.root, self.app.config.theme_mode)

    def _validate(self) -> None:
        index = self.subscription_choice.current()
        if index < 0:
            return
        subscription = self.subscriptions[index]
        self.validate_button.state(["disabled"])
        self.finish_button.state(["disabled"])
        self.validation_status.set("Verifying live, read-only access to Azure…")

        def worker() -> None:
            success, message = validate_subscription_access(subscription)
            self.window.after(
                0, lambda: self._validation_finished(subscription, success, message)
            )

        threading.Thread(
            target=worker, name="azure-access-validation", daemon=True
        ).start()

    def _validation_finished(
        self, subscription: AzureSubscription, success: bool, message: str
    ) -> None:
        self.validation_status.set(message)
        self.validate_button.state(["!disabled"])
        if success:
            self.selected_subscription = subscription
            self.finish_button.state(["!disabled"])
        else:
            self.selected_subscription = None

    def _finish(self) -> None:
        if not self.selected_subscription:
            return
        try:
            self.app.complete_setup(self.selected_subscription)
        except Exception as error:
            messagebox.showerror(
                "Could not finish setup", str(error), parent=self.window
            )
            return
        self.window.destroy()
        self.app.setup_window = None

    def _close(self) -> None:
        self.window.destroy()
        self.app.setup_window = None


class CheckManager:
    def __init__(self, app: BeaconApp) -> None:
        self.app = app
        self.window = Toplevel(app.root)
        apply_window_branding(self.window)
        self.window.title("Azure Health Beacon — Rule Studio")
        self.window.geometry("1180x790")
        self.window.minsize(980, 680)
        self.working = load_config()
        self.selected_id: str | None = None
        self.draft_id = str(uuid.uuid4())
        self.kind = StringVar(value=SIGNAL_SOURCES[0].label)
        self.name = StringVar()
        self.reference = StringVar()
        self.tenant = StringVar()
        self.expected = StringVar(value="Succeeded")
        self.template = StringVar(value="Active Azure Monitor alerts")
        self.workspace_id = StringVar()
        self.lookback = StringVar(value="5")
        self.metric_name = StringVar()
        self.metric_namespace = StringVar()
        self.metric_aggregation = StringVar(value="Average")
        self.metric_reducer = StringVar(value=METRIC_REDUCERS["latest"])
        self.metric_operator = StringVar(value=METRIC_OPERATORS["gt"])
        self.metric_threshold = StringVar(value="0")
        self.metric_filter = StringVar()
        self.property_path = StringVar(value="properties.provisioningState")
        self.property_operator = StringVar(
            value=PROPERTY_OPERATORS["equals_any"]
        )
        self.source_description = StringVar(value=SIGNAL_SOURCES[0].description)
        self.enabled = BooleanVar(value=True)
        self.last_tested_fingerprint: tuple[object, ...] | None = None
        self.test_status = StringVar(
            value="Define a check, then test it before applying."
        )
        self._build()
        for variable in (
            self.kind,
            self.name,
            self.reference,
            self.tenant,
            self.expected,
            self.workspace_id,
            self.lookback,
            self.metric_name,
            self.metric_namespace,
            self.metric_aggregation,
            self.metric_reducer,
            self.metric_operator,
            self.metric_threshold,
            self.metric_filter,
            self.property_path,
            self.property_operator,
            self.enabled,
        ):
            variable.trace_add("write", self._invalidate_test)
        self.kind.trace_add("write", self._kind_changed)
        self._refresh_list()
        self._kind_changed()
        apply_theme(app.root, app.config.theme_mode)

    def _build(self) -> None:
        outer = ttk.Frame(self.window, padding=20)
        outer.pack(fill=BOTH, expand=True)
        header = ttk.Frame(outer)
        header.pack(fill=X, pady=(0, 16))
        self.app.theme_button(header).pack(side=RIGHT)
        ttk.Label(header, text="Rule Studio", style="Title.TLabel").pack(anchor="w")
        ttk.Label(
            header,
            text="Browse what Azure exposes, define what should be a finding, test it live, then enable it.",
            style="Muted.TLabel",
        ).pack(anchor="w", pady=(3, 0))

        body = ttk.Panedwindow(outer, orient="horizontal")
        body.pack(fill=BOTH, expand=True)
        left = ttk.Frame(body, padding=(0, 0, 14, 0))
        body.add(left, weight=1)
        ttk.Label(left, text="YOUR RULES", style="Section.TLabel").pack(anchor="w")
        self.rule_tree = ttk.Treeview(
            left, columns=("source", "state"), show="tree headings", height=22
        )
        self.rule_tree.heading("#0", text="Name")
        self.rule_tree.heading("source", text="Source")
        self.rule_tree.heading("state", text="State")
        self.rule_tree.column("#0", width=180, minwidth=130)
        self.rule_tree.column("source", width=80, minwidth=70)
        self.rule_tree.column("state", width=65, minwidth=60)
        self.rule_tree.pack(fill=BOTH, expand=True, pady=(8, 10))
        self.rule_tree.bind("<<TreeviewSelect>>", self._select)
        ttk.Button(
            left, text="＋  New rule", style="Accent.TButton", command=self._new
        ).pack(fill=X)
        ttk.Button(left, text="Discover Azure signals…", command=self._discover).pack(
            fill=X, pady=(7, 0)
        )
        ttk.Button(left, text="Delete selected", command=self._remove).pack(
            fill=X, pady=(6, 0)
        )
        ttk.Separator(left).pack(fill=X, pady=12)
        ttk.Button(left, text="Import rule pack…", command=self._import_rules).pack(
            fill=X
        )
        ttk.Button(left, text="Export all rules…", command=self._export_rules).pack(
            fill=X, pady=(6, 0)
        )

        form = ttk.Frame(body, padding=(18, 0, 0, 0))
        body.add(form, weight=3)
        ttk.Label(form, text="RULE DEFINITION", style="Section.TLabel").pack(anchor="w")
        ttk.Label(form, text="Signal source").pack(anchor="w", pady=(10, 2))
        self.kind_choice = ttk.Combobox(
            form,
            textvariable=self.kind,
            values=tuple(source.label for source in SIGNAL_SOURCES),
            state="readonly",
        )
        self.kind_choice.pack(fill=X)
        ttk.Label(
            form,
            textvariable=self.source_description,
            style="Muted.TLabel",
            wraplength=680,
        ).pack(anchor="w", pady=(3, 0))
        self._field(form, "Rule name (editable)", self.name)

        self.provisioning_form = ttk.Frame(form)
        self._field(
            self.provisioning_form,
            "Azure Portal URL or resource ID",
            self.reference,
        )
        self._field(
            self.provisioning_form,
            "Tenant ID/domain (optional safety pin)",
            self.tenant,
        )
        self._field(
            self.provisioning_form,
            "Healthy provisioning states",
            self.expected,
        )

        self.vm_form = ttk.Frame(form)
        ttk.Label(
            self.vm_form,
            text="Reads the VM's live instance view rather than its provisioning state.",
            wraplength=680,
        ).pack(anchor="w", pady=(10, 2))
        self._field(self.vm_form, "Azure Portal URL or VM resource ID", self.reference)
        ttk.Button(
            self.vm_form,
            text="Browse Azure resources…",
            command=self._discover,
        ).pack(anchor="w", pady=(7, 0))
        self._field(
            self.vm_form,
            "Healthy VM power states (comma separated)",
            self.expected,
        )
        ttk.Label(
            self.vm_form,
            text="Common values: " + ", ".join(VM_POWER_STATES),
            style="Muted.TLabel",
            wraplength=680,
        ).pack(anchor="w", pady=(3, 0))

        self.property_form = ttk.Frame(form)
        ttk.Label(
            self.property_form,
            text="Advanced: compare a value from the resource's ARM JSON document. This does not expose secret values from data-plane services.",
            wraplength=680,
        ).pack(anchor="w", pady=(10, 2))
        self._field(
            self.property_form,
            "Azure Portal URL or resource ID",
            self.reference,
        )
        ttk.Button(
            self.property_form,
            text="Browse Azure resources…",
            command=self._discover,
        ).pack(anchor="w", pady=(7, 0))
        self._field(
            self.property_form,
            "Property path (for example properties.provisioningState)",
            self.property_path,
        )
        ttk.Label(self.property_form, text="Healthy when the property").pack(
            anchor="w", pady=(10, 2)
        )
        ttk.Combobox(
            self.property_form,
            textvariable=self.property_operator,
            values=tuple(PROPERTY_OPERATORS.values()),
            state="readonly",
        ).pack(fill=X)
        self._field(
            self.property_form,
            "Comparison values (comma separated; not used for Exists / Is missing)",
            self.expected,
        )

        self.graph_form = ttk.Frame(form)
        ttk.Label(
            self.graph_form,
            text="Searches every enabled subscription available to this login. Return one row per problem.",
            wraplength=540,
        ).pack(anchor="w", pady=(10, 6))
        self.log_form = ttk.Frame(form)
        ttk.Label(
            self.log_form,
            text="Full Azure Monitor KQL for logs and workspace-based Application Insights.",
            wraplength=680,
        ).pack(anchor="w", pady=(10, 4))
        self._field(self.log_form, "Log Analytics workspace ID", self.workspace_id)
        self._field(self.log_form, "Lookback window (minutes)", self.lookback)
        ttk.Button(
            self.log_form,
            text="Browse workspaces and tables…",
            command=self._discover,
        ).pack(anchor="w", pady=(8, 0))

        self.query_form = ttk.Frame(form)
        self.query_help = ttk.Label(
            self.query_form,
            text="Zero rows is healthy; every returned row becomes a red finding.",
            wraplength=680,
        )
        self.query_help.pack(anchor="w", pady=(8, 6))
        ttk.Label(self.query_form, text="Starting template").pack(anchor="w")
        template_values = (*GRAPH_TEMPLATES.keys(), "Custom KQL findings query")
        self.template_choice = ttk.Combobox(
            self.query_form,
            textvariable=self.template,
            values=template_values,
            state="readonly",
        )
        self.template_choice.pack(fill=X, pady=(2, 8))
        self.template_choice.bind("<<ComboboxSelected>>", self._apply_template)
        self.query_label = ttk.Label(self.query_form, text="KQL findings query")
        self.query_label.pack(anchor="w")
        self.query_text = Text(
            self.query_form,
            height=13,
            wrap="none",
            font=("Cascadia Mono", 9),
            undo=True,
        )
        self.query_text.pack(fill=BOTH, expand=True, pady=(2, 0))
        self.query_text.bind("<<Modified>>", self._query_modified)
        self._set_query(GRAPH_TEMPLATES["Active Azure Monitor alerts"])

        self.metric_form = ttk.Frame(form)
        ttk.Label(
            self.metric_form,
            text="Read any Azure Monitor metric exposed by a resource and trip when the condition is true.",
            wraplength=680,
        ).pack(anchor="w", pady=(10, 2))
        self._field(self.metric_form, "Azure Portal URL or resource ID", self.reference)
        ttk.Button(
            self.metric_form,
            text="Browse resources and metric definitions…",
            command=self._discover,
        ).pack(anchor="w", pady=(7, 0))
        self._field(self.metric_form, "Metric name", self.metric_name)
        self._field(
            self.metric_form, "Metric namespace (optional)", self.metric_namespace
        )
        metric_row = ttk.Frame(self.metric_form)
        metric_row.pack(fill=X, pady=(10, 0))
        ttk.Label(metric_row, text="Azure aggregation").grid(
            row=0, column=0, sticky="w"
        )
        ttk.Label(metric_row, text="Evaluate").grid(
            row=0, column=1, sticky="w", padx=(10, 0)
        )
        ttk.Label(metric_row, text="Condition").grid(
            row=0, column=2, sticky="w", padx=(10, 0)
        )
        ttk.Label(metric_row, text="Threshold").grid(
            row=0, column=3, sticky="w", padx=(10, 0)
        )
        ttk.Combobox(
            metric_row,
            textvariable=self.metric_aggregation,
            values=("Average", "Count", "Maximum", "Minimum", "Total"),
            state="readonly",
            width=12,
        ).grid(row=1, column=0, sticky="ew")
        ttk.Combobox(
            metric_row,
            textvariable=self.metric_reducer,
            values=tuple(METRIC_REDUCERS.values()),
            state="readonly",
            width=10,
        ).grid(row=1, column=1, sticky="ew", padx=(10, 0))
        ttk.Combobox(
            metric_row,
            textvariable=self.metric_operator,
            values=tuple(METRIC_OPERATORS.values()),
            state="readonly",
            width=7,
        ).grid(row=1, column=2, sticky="ew", padx=(10, 0))
        ttk.Entry(metric_row, textvariable=self.metric_threshold, width=12).grid(
            row=1, column=3, sticky="ew", padx=(10, 0)
        )
        for column in range(4):
            metric_row.columnconfigure(column, weight=1)
        self._field(self.metric_form, "Lookback window (minutes)", self.lookback)
        self._field(
            self.metric_form,
            "Dimension filter (optional, for example ApiName eq 'GetBlob')",
            self.metric_filter,
        )

        self.enabled_check = ttk.Checkbutton(
            form, text="Enabled", variable=self.enabled
        )
        self.enabled_check.pack(anchor="w", pady=(10, 0))
        ttk.Label(form, textvariable=self.test_status, wraplength=540).pack(
            anchor="w", pady=(14, 8)
        )
        actions = ttk.Frame(form)
        actions.pack(fill=X, pady=(8, 0))
        ttk.Button(actions, text="Test without saving", command=self._test).pack(
            side=LEFT
        )
        self.apply_button = ttk.Button(
            actions,
            text="Save tested rule",
            style="Accent.TButton",
            command=self._save_check,
        )
        self.apply_button.pack(side=RIGHT)
        self.apply_button.state(["disabled"])
        ttk.Separator(form).pack(fill=X, pady=16)
        ttk.Label(
            form,
            text=f"Stored locally in {config_path()}\nNo Azure credentials or tokens are stored here.",
            wraplength=540,
        ).pack(anchor="w")

    @staticmethod
    def _field(parent: ttk.Frame, label: str, variable: StringVar) -> None:
        ttk.Label(parent, text=label).pack(anchor="w", pady=(10, 2))
        ttk.Entry(parent, textvariable=variable).pack(fill=X)

    def _refresh_list(self) -> None:
        for item in self.rule_tree.get_children():
            self.rule_tree.delete(item)
        for check in self.working.checks:
            source = SOURCE_BY_KEY.get(check.kind, SIGNAL_SOURCES[0])
            short_label = {
                "Provisioning state": "Provisioning",
                "VM power state": "VM power",
                "Resource property (advanced)": "Property",
                "Resource Graph": "Graph",
                "Logs / Application Insights": "Logs",
                "Azure Monitor metric": "Metric",
            }.get(source.label, source.label)
            self.rule_tree.insert(
                "",
                END,
                iid=check.id,
                text=check.name,
                values=(short_label, "On" if check.enabled else "Off"),
            )

    def _select(self, _event: object = None) -> None:
        selection = self.rule_tree.selection()
        if not selection:
            return
        check = next(
            (item for item in self.working.checks if item.id == selection[0]), None
        )
        if check is None:
            return
        self.selected_id = check.id
        self.kind.set(SOURCE_BY_KEY.get(check.kind, SIGNAL_SOURCES[0]).label)
        self.name.set(check.name)
        self.reference.set(check.portal_url or check.resource_id)
        self.tenant.set(check.tenant_id)
        self.expected.set(", ".join(check.expected_values))
        self.workspace_id.set(check.workspace_id)
        self.lookback.set(str(check.lookback_minutes))
        self.metric_name.set(check.metric_name)
        self.metric_namespace.set(check.metric_namespace)
        self.metric_aggregation.set(check.metric_aggregation)
        self.metric_reducer.set(
            METRIC_REDUCERS.get(check.metric_reducer, METRIC_REDUCERS["latest"])
        )
        self.metric_operator.set(
            METRIC_OPERATORS.get(check.metric_operator, METRIC_OPERATORS["gt"])
        )
        self.metric_threshold.set(f"{check.metric_threshold:g}")
        self.metric_filter.set(check.metric_filter)
        self.property_path.set(check.property_path)
        self.property_operator.set(
            PROPERTY_OPERATORS.get(
                check.property_operator, PROPERTY_OPERATORS["equals_any"]
            )
        )
        self._set_query(check.query)
        self.template.set(
            "Custom log query"
            if check.kind == "azure_log_analytics"
            else "Custom KQL findings query"
        )
        self.enabled.set(check.enabled)
        self.test_status.set(
            "Edit or rename anything, then test before saving changes."
        )

    def _new(self) -> None:
        self.selected_id = None
        self.draft_id = str(uuid.uuid4())
        self.kind.set(SIGNAL_SOURCES[0].label)
        self.name.set("")
        self.reference.set("")
        self.tenant.set("")
        self.expected.set("Succeeded")
        self.template.set("Active Azure Monitor alerts")
        self._set_query(GRAPH_TEMPLATES["Active Azure Monitor alerts"])
        self.workspace_id.set("")
        self.lookback.set("5")
        self.metric_name.set("")
        self.metric_namespace.set("")
        self.metric_aggregation.set("Average")
        self.metric_reducer.set(METRIC_REDUCERS["latest"])
        self.metric_operator.set(METRIC_OPERATORS["gt"])
        self.metric_threshold.set("0")
        self.metric_filter.set("")
        self.property_path.set("properties.provisioningState")
        self.property_operator.set(PROPERTY_OPERATORS["equals_any"])
        self.enabled.set(True)
        self.rule_tree.selection_remove(*self.rule_tree.selection())
        self.test_status.set("Choose a signal source or open Discover Azure signals.")

    def _kind_changed(self, *_args: object) -> None:
        if not hasattr(self, "provisioning_form"):
            return
        self.provisioning_form.pack_forget()
        self.vm_form.pack_forget()
        self.property_form.pack_forget()
        self.graph_form.pack_forget()
        self.log_form.pack_forget()
        self.metric_form.pack_forget()
        self.query_form.pack_forget()
        source_key = SOURCE_KEY_BY_LABEL.get(
            self.kind.get(), "azure_resource_provisioning"
        )
        self.source_description.set(SOURCE_BY_KEY[source_key].description)
        if source_key == "azure_resource_graph":
            self.graph_form.pack(fill=X, before=self.enabled_check)
            self.query_form.pack(fill=BOTH, expand=True, before=self.enabled_check)
            values = (*GRAPH_TEMPLATES.keys(), "Custom KQL findings query")
            self.template_choice.configure(values=values)
            if self.template.get() not in values:
                self.template.set("Custom KQL findings query")
            self.query_label.configure(text="Resource Graph KQL findings query")
            self.query_help.configure(
                text="Zero rows is healthy; every returned row becomes a confirmed finding. Runs across accessible subscriptions."
            )
            self.test_status.set(
                "The query is read-only. Test it live before applying the rule."
            )
        elif source_key == "azure_log_analytics":
            self.log_form.pack(fill=X, before=self.enabled_check)
            self.query_form.pack(fill=BOTH, expand=True, before=self.enabled_check)
            values = (*LOG_TEMPLATES.keys(), "Custom log query")
            self.template_choice.configure(values=values)
            if self.template.get() not in values:
                self.template.set("Custom log query")
            self.query_label.configure(text="Azure Monitor / Application Insights KQL")
            self.query_help.configure(
                text="Return one row per problem. Query errors or missing table access are grey, not green."
            )
            self.test_status.set(
                "Select a workspace, write or choose KQL, then test against live logs."
            )
        elif source_key == "azure_monitor_metric":
            self.metric_form.pack(fill=BOTH, expand=True, before=self.enabled_check)
            self.test_status.set(
                "Browse a resource's metric definitions, choose a condition, then test it."
            )
        elif source_key == "azure_vm_power_state":
            if self.expected.get().strip() in {"", "Succeeded"}:
                self.expected.set("PowerState/running")
            self.vm_form.pack(fill=X, before=self.enabled_check)
            self.test_status.set(
                "Choose a VM and the power states that count as healthy, then test it live."
            )
        elif source_key == "azure_resource_property":
            if self.expected.get().strip().startswith("PowerState/"):
                self.expected.set("")
            self.property_form.pack(fill=X, before=self.enabled_check)
            self.test_status.set(
                "Choose a resource, property path, comparison, and values, then test it live."
            )
        else:
            if self.expected.get().strip().startswith("PowerState/"):
                self.expected.set("Succeeded")
            self.provisioning_form.pack(fill=X, before=self.enabled_check)
            self.test_status.set("Paste an Azure Portal resource URL or resource ID.")
        apply_theme(self.app.root, self.app.config.theme_mode)

    def _set_query(self, value: str) -> None:
        self.query_text.delete("1.0", END)
        self.query_text.insert("1.0", value)
        self.query_text.edit_modified(False)

    def _query_modified(self, _event: object = None) -> None:
        if self.query_text.edit_modified():
            self.query_text.edit_modified(False)
            self._invalidate_test()

    def _apply_template(self, _event: object = None) -> None:
        selected = self.template.get()
        if SOURCE_KEY_BY_LABEL.get(self.kind.get()) == "azure_log_analytics":
            query = LOG_TEMPLATES.get(selected, CUSTOM_LOG_TEMPLATE)
        else:
            query = GRAPH_TEMPLATES.get(selected, CUSTOM_TEMPLATE)
        self._set_query(query)
        if not self.name.get().strip():
            self.name.set(selected.replace("Azure ", ""))
        self._invalidate_test()

    def _invalidate_test(self, *_args: object) -> None:
        self.last_tested_fingerprint = None
        if hasattr(self, "apply_button"):
            self.apply_button.state(["disabled"])

    @staticmethod
    def _fingerprint(definition: CheckDefinition) -> tuple[object, ...]:
        return (
            definition.id,
            definition.name,
            definition.resource_id,
            definition.portal_url,
            definition.tenant_id,
            tuple(definition.expected_values),
            definition.enabled,
            definition.kind,
            definition.query,
            definition.scope,
            definition.workspace_id,
            definition.lookback_minutes,
            definition.metric_name,
            definition.metric_namespace,
            definition.metric_aggregation,
            definition.metric_reducer,
            definition.metric_operator,
            definition.metric_threshold,
            definition.metric_filter,
            definition.property_path,
            definition.property_operator,
        )

    def _definition_from_form(self) -> CheckDefinition:
        source_key = SOURCE_KEY_BY_LABEL.get(
            self.kind.get(), "azure_resource_provisioning"
        )
        if source_key == "azure_resource_graph":
            definition = CheckDefinition(
                id=self.selected_id or self.draft_id,
                name=self.name.get().strip(),
                resource_id="",
                portal_url="",
                tenant_id="",
                expected_values=[],
                enabled=self.enabled.get(),
                kind="azure_resource_graph",
                query=self.query_text.get("1.0", "end-1c"),
                scope="all_accessible",
            )
            validate_definition(definition)
            return definition
        if source_key == "azure_log_analytics":
            definition = CheckDefinition(
                id=self.selected_id or self.draft_id,
                name=self.name.get().strip(),
                resource_id="",
                portal_url="",
                tenant_id="",
                expected_values=[],
                enabled=self.enabled.get(),
                kind="azure_log_analytics",
                query=self.query_text.get("1.0", "end-1c"),
                scope="workspace",
                workspace_id=self.workspace_id.get().strip(),
                lookback_minutes=int(self.lookback.get()),
            )
            validate_definition(definition)
            return definition
        if source_key == "azure_monitor_metric":
            resource_id, portal_url, _tenant_hint = parse_resource_reference(
                self.reference.get()
            )
            definition = CheckDefinition(
                id=self.selected_id or self.draft_id,
                name=self.name.get().strip(),
                resource_id=resource_id,
                portal_url=portal_url,
                expected_values=[],
                enabled=self.enabled.get(),
                kind="azure_monitor_metric",
                scope="resource",
                lookback_minutes=int(self.lookback.get()),
                metric_name=self.metric_name.get().strip(),
                metric_namespace=self.metric_namespace.get().strip(),
                metric_aggregation=self.metric_aggregation.get(),
                metric_reducer=METRIC_REDUCER_BY_LABEL[self.metric_reducer.get()],
                metric_operator=METRIC_OPERATOR_BY_LABEL[self.metric_operator.get()],
                metric_threshold=float(self.metric_threshold.get()),
                metric_filter=self.metric_filter.get().strip(),
            )
            validate_definition(definition)
            return definition
        resource_id, portal_url, tenant_hint = parse_resource_reference(
            self.reference.get()
        )
        tenant_from_url = tenant_hint if self._looks_like_uuid(tenant_hint) else ""
        segments = [segment for segment in resource_id.split("/") if segment]
        resource_subscription = (
            segments[1]
            if len(segments) > 1 and segments[0].casefold() == "subscriptions"
            else ""
        )
        selected_tenant = (
            self.app.config.azure_tenant_id
            if resource_subscription.casefold()
            == self.app.config.azure_subscription_id.casefold()
            else ""
        )
        definition = CheckDefinition(
            id=self.selected_id or self.draft_id,
            name=self.name.get().strip(),
            resource_id=resource_id,
            portal_url=portal_url,
            tenant_id=self.tenant.get().strip() or tenant_from_url or selected_tenant,
            expected_values=[
                item.strip() for item in self.expected.get().split(",") if item.strip()
            ],
            enabled=self.enabled.get(),
            kind=source_key,
            property_path=(
                self.property_path.get().strip()
                if source_key == "azure_resource_property"
                else ""
            ),
            property_operator=(
                PROPERTY_OPERATOR_BY_LABEL[self.property_operator.get()]
                if source_key == "azure_resource_property"
                else "equals_any"
            ),
        )
        validate_definition(definition)
        return definition

    @staticmethod
    def _looks_like_uuid(value: str) -> bool:
        try:
            uuid.UUID(value)
        except (ValueError, AttributeError):
            return False
        return True

    def _test(self) -> None:
        try:
            definition = self._definition_from_form()
            if not definition.name:
                raise ValueError("Enter a friendly name first")
        except ValueError as error:
            messagebox.showerror("Cannot test check", str(error), parent=self.window)
            return
        self.test_status.set(
            "Testing with Azure Health Beacon's isolated Azure connection…"
        )
        tested_fingerprint = self._fingerprint(definition)
        self.last_tested_fingerprint = None
        self.apply_button.state(["disabled"])

        def worker() -> None:
            result = run_check(
                definition,
                timeout_seconds=self.working.timeout_seconds,
                retry_count=self.working.retry_count,
            )
            self.window.after(
                0, lambda: self._test_finished(result, tested_fingerprint)
            )

        threading.Thread(target=worker, daemon=True).start()

    def _test_finished(
        self, result: CheckResult, tested_fingerprint: tuple[object, ...]
    ) -> None:
        message = f"{result.state.value.upper()}: {result.summary}"
        if result.findings:
            names = ", ".join(item.title for item in result.findings[:3])
            message += f" Sample: {names}"
        self.test_status.set(message)
        try:
            current = self._fingerprint(self._definition_from_form())
        except ValueError:
            return
        if (
            current == tested_fingerprint
            and result.state is not CheckState.UNCONNECTABLE
        ):
            self.last_tested_fingerprint = tested_fingerprint
            self.apply_button.state(["!disabled"])

    def _save_working(self) -> None:
        # Preserve settings that can change while Rule Studio is open.
        latest = load_config()
        latest.checks = self.working.checks
        save_config(latest)
        self.working = latest

    def _save_check(self) -> None:
        try:
            definition = self._definition_from_form()
            if not definition.name:
                raise ValueError("Enter a friendly name")
            if self.last_tested_fingerprint != self._fingerprint(definition):
                raise ValueError(
                    "Test the current rule successfully before applying it"
                )
            existing = next(
                (
                    index
                    for index, item in enumerate(self.working.checks)
                    if item.id == definition.id
                ),
                None,
            )
            if existing is None:
                self.working.checks.append(definition)
            else:
                self.working.checks[existing] = definition
            self._save_working()
        except Exception as error:
            messagebox.showerror("Could not save check", str(error), parent=self.window)
            return
        self.selected_id = definition.id
        self.test_status.set("Applied. A fresh background check has been queued.")
        self.last_tested_fingerprint = self._fingerprint(definition)
        self._refresh_list()
        self.app.reload_config()

    def _remove(self) -> None:
        selection = self.rule_tree.selection()
        if not selection:
            return
        check = next(
            (item for item in self.working.checks if item.id == selection[0]), None
        )
        if check is None:
            return
        if not messagebox.askyesno(
            "Remove check", f"Remove ‘{check.name}’?", parent=self.window
        ):
            return
        self.working.checks = [
            item for item in self.working.checks if item.id != check.id
        ]
        self._save_working()
        self._new()
        self._refresh_list()
        self.app.reload_config()

    def _discover(self) -> None:
        SignalExplorer(self)

    def use_workspace(self, workspace: AzureWorkspace, table: str = "") -> None:
        self.kind.set(SOURCE_BY_KEY["azure_log_analytics"].label)
        self.workspace_id.set(workspace.customer_id)
        if table:
            self.template.set("Custom log query")
            self._set_query(f"{table}\n| where TimeGenerated > ago(5m)\n| take 10")
            if not self.name.get().strip():
                self.name.set(f"{table} findings")
        self.test_status.set(
            f"Selected workspace {workspace.name}. Review the query and test it live."
        )

    def use_metric(
        self, resource: AzureResource, metric: AzureMetricDefinition
    ) -> None:
        self.kind.set(SOURCE_BY_KEY["azure_monitor_metric"].label)
        self.reference.set(resource.resource_id)
        self.metric_name.set(metric.name)
        self.metric_namespace.set(metric.namespace)
        if metric.aggregations:
            preferred = next(
                (
                    value
                    for value in ("Average", "Maximum", "Total", "Count", "Minimum")
                    if value in metric.aggregations
                ),
                metric.aggregations[0],
            )
            self.metric_aggregation.set(preferred)
        if not self.name.get().strip():
            self.name.set(f"{resource.name} — {metric.display_name}")
        dimensions = ", ".join(metric.dimensions) or "none"
        self.test_status.set(
            f"Selected {metric.display_name} ({metric.unit or 'unitless'}). Available dimensions: {dimensions}."
        )

    def use_resource(self, resource: AzureResource) -> None:
        source_key = SOURCE_KEY_BY_LABEL.get(self.kind.get(), "")
        if source_key not in {
            "azure_resource_provisioning",
            "azure_resource_property",
            "azure_vm_power_state",
        }:
            self.kind.set(SOURCE_BY_KEY["azure_resource_property"].label)
            source_key = "azure_resource_property"
        self.reference.set(resource.resource_id)
        if not self.name.get().strip():
            suffix = {
                "azure_resource_provisioning": "provisioning",
                "azure_resource_property": "property",
                "azure_vm_power_state": "power state",
            }[source_key]
            self.name.set(f"{resource.name} — {suffix}")
        self.test_status.set(
            f"Selected {resource.name} ({resource.resource_type}). Complete the condition and test it live."
        )

    def _export_rules(self) -> None:
        target = filedialog.asksaveasfilename(
            parent=self.window,
            title="Export Azure Health Beacon rules",
            defaultextension=".ahbrules.json",
            filetypes=[
                ("Azure Health Beacon rules", "*.ahbrules.json"),
                ("JSON", "*.json"),
            ],
        )
        if not target:
            return
        try:
            export_rule_pack(Path(target), self.working.checks)
        except Exception as error:
            messagebox.showerror(
                "Could not export rules", str(error), parent=self.window
            )
            return
        messagebox.showinfo(
            "Rules exported",
            "The rule pack contains resource identifiers, expected states, and KQL text, but no credentials or tokens. Review query text before sharing it because it may reveal internal names.",
            parent=self.window,
        )

    def _import_rules(self) -> None:
        source = filedialog.askopenfilename(
            parent=self.window,
            title="Import Azure Health Beacon rules",
            filetypes=[
                ("Azure Health Beacon rules", "*.ahbrules.json"),
                ("JSON", "*.json"),
            ],
        )
        if not source:
            return
        try:
            imported = import_rule_pack(Path(source))
        except Exception as error:
            messagebox.showerror("Rule pack rejected", str(error), parent=self.window)
            return
        preview = "\n".join(f"• {check.name}" for check in imported[:8])
        if len(imported) > 8:
            preview += f"\n• …and {len(imported) - 8} more"
        confirmed = messagebox.askyesno(
            "Import disabled rules?",
            f"Import {len(imported)} data-only rules?\n\n{preview}\n\n"
            "For safety, imported rules remain disabled until you review, test, and enable them.",
            parent=self.window,
        )
        if not confirmed:
            return
        by_id = {check.id: index for index, check in enumerate(self.working.checks)}
        for check in imported:
            if check.id in by_id:
                self.working.checks[by_id[check.id]] = check
            else:
                self.working.checks.append(check)
        try:
            self._save_working()
        except Exception as error:
            messagebox.showerror(
                "Could not save imported rules", str(error), parent=self.window
            )
            return
        self._refresh_list()
        self.app.reload_config()
        self.test_status.set(
            f"Imported {len(imported)} disabled rules. Review and test before enabling."
        )


class SignalExplorer:
    """Read-only catalogue of signal surfaces available to the signed-in identity."""

    def __init__(self, manager: CheckManager) -> None:
        self.manager = manager
        self.app = manager.app
        self.window = Toplevel(manager.window)
        apply_window_branding(self.window)
        self.window.title("Discover Azure signals")
        self.window.geometry("1050x720")
        self.window.minsize(900, 620)
        self.workspaces: list[AzureWorkspace] = []
        self.resources: list[AzureResource] = []
        self.metrics: list[AzureMetricDefinition] = []
        self.selected_workspace: AzureWorkspace | None = None
        self.selected_resource: AzureResource | None = None
        self.status = StringVar(
            value="Nothing is downloaded or persisted. Discovery reads only metadata visible to your Azure identity."
        )
        self._build()
        apply_theme(self.app.root, self.app.config.theme_mode)

    def _build(self) -> None:
        outer = ttk.Frame(self.window, padding=20)
        outer.pack(fill=BOTH, expand=True)
        header = ttk.Frame(outer)
        header.pack(fill=X, pady=(0, 14))
        self.app.theme_button(header).pack(side=RIGHT)
        ttk.Label(header, text="Discover Azure signals", style="Title.TLabel").pack(
            anchor="w"
        )
        ttk.Label(
            header,
            text="Start from Azure's live schemas and definitions, then decide what matters.",
            style="Muted.TLabel",
        ).pack(anchor="w", pady=(3, 0))
        notebook = ttk.Notebook(outer)
        notebook.pack(fill=BOTH, expand=True)
        self._build_logs_tab(notebook)
        self._build_metrics_tab(notebook)
        self._build_graph_tab(notebook)
        ttk.Label(outer, textvariable=self.status, style="Muted.TLabel").pack(
            fill=X, pady=(12, 0)
        )

    def _build_logs_tab(self, notebook: ttk.Notebook) -> None:
        tab = ttk.Frame(notebook, padding=14)
        notebook.add(tab, text="Logs & Application Insights")
        ttk.Label(
            tab,
            text="Choose a workspace, inspect the tables its schema exposes, then use a table as the beginning of your own KQL rule.",
            wraplength=900,
        ).pack(anchor="w", pady=(0, 10))
        panes = ttk.Panedwindow(tab, orient="horizontal")
        panes.pack(fill=BOTH, expand=True)
        workspace_frame = ttk.Frame(panes, padding=(0, 0, 10, 0))
        table_frame = ttk.Frame(panes, padding=(10, 0, 0, 0))
        panes.add(workspace_frame, weight=2)
        panes.add(table_frame, weight=1)
        ttk.Label(
            workspace_frame, text="Readable workspaces", style="Section.TLabel"
        ).pack(anchor="w")
        self.workspace_tree = ttk.Treeview(
            workspace_frame,
            columns=("group", "subscription"),
            show="tree headings",
        )
        self.workspace_tree.heading("#0", text="Workspace")
        self.workspace_tree.heading("group", text="Resource group")
        self.workspace_tree.heading("subscription", text="Subscription")
        self.workspace_tree.column("#0", width=190)
        self.workspace_tree.column("group", width=160)
        self.workspace_tree.column("subscription", width=220)
        self.workspace_tree.pack(fill=BOTH, expand=True, pady=(8, 8))
        self.workspace_tree.bind("<<TreeviewSelect>>", self._workspace_selected)
        ttk.Button(
            workspace_frame,
            text="Load accessible workspaces",
            style="Accent.TButton",
            command=self._load_workspaces,
        ).pack(fill=X)
        ttk.Label(table_frame, text="Available tables", style="Section.TLabel").pack(
            anchor="w"
        )
        self.table_tree = ttk.Treeview(table_frame, show="tree")
        self.table_tree.pack(fill=BOTH, expand=True, pady=(8, 8))
        self.table_tree.bind("<Double-1>", lambda _event: self._use_table())
        ttk.Button(
            table_frame,
            text="Use selected table",
            command=self._use_table,
        ).pack(fill=X)

    def _build_metrics_tab(self, notebook: ttk.Notebook) -> None:
        tab = ttk.Frame(notebook, padding=14)
        notebook.add(tab, text="Metrics")
        ttk.Label(
            tab,
            text="Select any readable ARM resource, then inspect the Azure Monitor metrics, aggregations, units, and dimensions it exposes.",
            wraplength=900,
        ).pack(anchor="w", pady=(0, 10))
        panes = ttk.Panedwindow(tab, orient="horizontal")
        panes.pack(fill=BOTH, expand=True)
        resource_frame = ttk.Frame(panes, padding=(0, 0, 10, 0))
        metric_frame = ttk.Frame(panes, padding=(10, 0, 0, 0))
        panes.add(resource_frame, weight=2)
        panes.add(metric_frame, weight=2)
        ttk.Label(
            resource_frame, text="Readable resources", style="Section.TLabel"
        ).pack(anchor="w")
        self.resource_tree = ttk.Treeview(
            resource_frame, columns=("type", "group"), show="tree headings"
        )
        self.resource_tree.heading("#0", text="Resource")
        self.resource_tree.heading("type", text="Type")
        self.resource_tree.heading("group", text="Resource group")
        self.resource_tree.column("#0", width=180)
        self.resource_tree.column("type", width=230)
        self.resource_tree.column("group", width=140)
        self.resource_tree.pack(fill=BOTH, expand=True, pady=(8, 8))
        self.resource_tree.bind("<<TreeviewSelect>>", self._resource_selected)
        ttk.Button(
            resource_frame,
            text="Load accessible resources",
            style="Accent.TButton",
            command=self._load_resources,
        ).pack(fill=X)
        ttk.Button(
            resource_frame,
            text="Use selected resource",
            command=self._use_resource,
        ).pack(fill=X, pady=(6, 0))
        ttk.Label(metric_frame, text="Metric definitions", style="Section.TLabel").pack(
            anchor="w"
        )
        self.metric_tree = ttk.Treeview(
            metric_frame, columns=("unit", "aggregations"), show="tree headings"
        )
        self.metric_tree.heading("#0", text="Metric")
        self.metric_tree.heading("unit", text="Unit")
        self.metric_tree.heading("aggregations", text="Aggregations")
        self.metric_tree.column("#0", width=190)
        self.metric_tree.column("unit", width=80)
        self.metric_tree.column("aggregations", width=170)
        self.metric_tree.pack(fill=BOTH, expand=True, pady=(8, 8))
        self.metric_tree.bind("<Double-1>", lambda _event: self._use_metric())
        ttk.Button(
            metric_frame, text="Use selected metric", command=self._use_metric
        ).pack(fill=X)

    def _build_graph_tab(self, notebook: ttk.Notebook) -> None:
        tab = ttk.Frame(notebook, padding=18)
        notebook.add(tab, text="Resource Graph")
        ttk.Label(
            tab,
            text="Resource Graph exposes inventory and control-plane data across subscriptions. Pick a transparent starter query or write your own in Rule Studio.",
            wraplength=880,
        ).pack(anchor="w", pady=(0, 12))
        self.graph_template = StringVar(value=next(iter(GRAPH_TEMPLATES)))
        choices = ttk.Treeview(tab, show="tree", height=10)
        choices.pack(fill=BOTH, expand=True)
        for name in GRAPH_TEMPLATES:
            choices.insert("", END, iid=name, text=name)
        choices.selection_set(self.graph_template.get())

        def use_graph() -> None:
            selection = choices.selection()
            if not selection:
                return
            name = selection[0]
            self.manager.kind.set(SOURCE_BY_KEY["azure_resource_graph"].label)
            self.manager.template.set(name)
            self.manager._set_query(GRAPH_TEMPLATES[name])
            if not self.manager.name.get().strip():
                self.manager.name.set(name)
            self.manager.test_status.set(
                "Starter query selected. Review every line and test it live before saving."
            )
            self.window.destroy()

        ttk.Button(
            tab, text="Use selected starter", style="Accent.TButton", command=use_graph
        ).pack(anchor="e", pady=(10, 0))

    def _load_workspaces(self) -> None:
        self.status.set("Reading accessible Log Analytics workspaces…")

        def worker() -> None:
            workspaces, errors = discover_workspaces()
            self.window.after(0, lambda: self._show_workspaces(workspaces, errors))

        threading.Thread(target=worker, name="discover-workspaces", daemon=True).start()

    def _show_workspaces(
        self, workspaces: list[AzureWorkspace], errors: list[str]
    ) -> None:
        self.workspaces = workspaces
        for item in self.workspace_tree.get_children():
            self.workspace_tree.delete(item)
        for index, workspace in enumerate(workspaces):
            self.workspace_tree.insert(
                "",
                END,
                iid=str(index),
                text=workspace.name,
                values=(workspace.resource_group, workspace.subscription_id),
            )
        suffix = f" Partial scope: {errors[0]}" if errors else ""
        self.status.set(f"Found {len(workspaces)} readable workspace(s).{suffix}")

    def _workspace_selected(self, _event: object = None) -> None:
        selection = self.workspace_tree.selection()
        if not selection:
            return
        self.selected_workspace = self.workspaces[int(selection[0])]
        self.status.set(f"Reading schema for {self.selected_workspace.name}…")

        def worker() -> None:
            tables, error = discover_workspace_tables(self.selected_workspace)
            self.window.after(0, lambda: self._show_tables(tables, error))

        threading.Thread(target=worker, name="discover-log-tables", daemon=True).start()

    def _show_tables(self, tables: list[str], error: str) -> None:
        for item in self.table_tree.get_children():
            self.table_tree.delete(item)
        for table in tables:
            self.table_tree.insert("", END, iid=table, text=table)
        self.status.set(
            error or f"Found {len(tables)} table(s) in the workspace schema."
        )

    def _use_table(self) -> None:
        selection = self.table_tree.selection()
        if not selection or self.selected_workspace is None:
            return
        self.manager.use_workspace(self.selected_workspace, selection[0])
        self.window.destroy()

    def _load_resources(self) -> None:
        self.status.set("Reading accessible Azure resources…")

        def worker() -> None:
            resources, errors = discover_resources()
            self.window.after(0, lambda: self._show_resources(resources, errors))

        threading.Thread(target=worker, name="discover-resources", daemon=True).start()

    def _show_resources(
        self, resources: list[AzureResource], errors: list[str]
    ) -> None:
        self.resources = resources
        for item in self.resource_tree.get_children():
            self.resource_tree.delete(item)
        for index, resource in enumerate(resources):
            self.resource_tree.insert(
                "",
                END,
                iid=str(index),
                text=resource.name,
                values=(resource.resource_type, resource.resource_group),
            )
        suffix = f" Partial scope: {errors[0]}" if errors else ""
        self.status.set(f"Found {len(resources)} readable resource(s).{suffix}")

    def _resource_selected(self, _event: object = None) -> None:
        selection = self.resource_tree.selection()
        if not selection:
            return
        self.selected_resource = self.resources[int(selection[0])]
        self.status.set(f"Reading metrics for {self.selected_resource.name}…")

        def worker() -> None:
            metrics, error = discover_metric_definitions(
                self.selected_resource.resource_id
            )
            self.window.after(0, lambda: self._show_metrics(metrics, error))

        threading.Thread(target=worker, name="discover-metrics", daemon=True).start()

    def _show_metrics(self, metrics: list[AzureMetricDefinition], error: str) -> None:
        self.metrics = metrics
        for item in self.metric_tree.get_children():
            self.metric_tree.delete(item)
        for index, metric in enumerate(metrics):
            self.metric_tree.insert(
                "",
                END,
                iid=str(index),
                text=metric.display_name,
                values=(metric.unit, ", ".join(metric.aggregations)),
            )
        self.status.set(error or f"Found {len(metrics)} metric definition(s).")

    def _use_resource(self) -> None:
        if self.selected_resource is None:
            return
        self.manager.use_resource(self.selected_resource)
        self.window.destroy()

    def _use_metric(self) -> None:
        selection = self.metric_tree.selection()
        if not selection or self.selected_resource is None:
            return
        self.manager.use_metric(self.selected_resource, self.metrics[int(selection[0])])
        self.window.destroy()


def configure_logging() -> None:
    target = log_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        target, maxBytes=1_000_000, backupCount=2, encoding="utf-8"
    )
    logging.basicConfig(
        level=logging.INFO,
        handlers=[handler],
        format="%(asctime)s %(levelname)s %(message)s",
    )


def main() -> int:
    instance = _acquire_single_instance()
    if instance is None:
        ctypes.windll.user32.MessageBoxW(
            None, "Azure Health Beacon is already running.", "Azure Health Beacon", 0x40
        )
        return 0
    configure_logging()
    try:
        BeaconApp(startup_launch="--startup" in sys.argv[1:]).run()
    except Exception:
        LOGGER.exception("Fatal application error")
        raise
    return 0


if __name__ == "__main__":
    sys.exit(main())
