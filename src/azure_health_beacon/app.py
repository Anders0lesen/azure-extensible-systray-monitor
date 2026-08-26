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
from logging.handlers import RotatingFileHandler
from pathlib import Path
from tkinter import (
    BOTH,
    END,
    LEFT,
    RIGHT,
    BooleanVar,
    StringVar,
    Tk,
    Toplevel,
    X,
    Y,
    filedialog,
    messagebox,
    ttk,
)

import pystray

from .azure import (
    AzureSubscription,
    delete_isolated_azure_state,
    interactive_login,
    list_subscriptions,
    run_provisioning_check,
    validate_subscription_access,
)
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
)
from .icons import icon_for
from .model import (
    BeaconState,
    CheckDefinition,
    CheckResult,
    CheckState,
    aggregate_state,
)

LOGGER = logging.getLogger(__name__)

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
    def __init__(self) -> None:
        self.root = Tk()
        self.root.withdraw()
        self.root.title("Azure Health Beacon")
        self.root.protocol("WM_DELETE_WINDOW", self.hide_status)
        self.config = self._load_config_safely()
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
        self.root.after(100, self._process_ui_events)
        self.root.after(125, self._animate)
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
                    run_provisioning_check,
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

    def _menu_exit(self, _icon: pystray.Icon, _item: pystray.MenuItem) -> None:
        self.ui_events.put(("exit", None))

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
        self.status_window = window
        window.title("Azure Health Beacon")
        window.geometry("560x420")
        window.minsize(500, 320)
        window.protocol("WM_DELETE_WINDOW", self.hide_status)
        header = ttk.Frame(window, padding=16)
        header.pack(fill=X)
        ttk.Label(
            header, textvariable=self._state_text(), font=("Segoe UI", 16, "bold")
        ).pack(anchor="w")
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
        ttk.Button(footer, text="Check now", command=self.check_now_event.set).pack(
            side=RIGHT
        )
        self._refresh_status()

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
            ttk.Label(card, text=state_text, font=("Segoe UI", 10, "bold")).pack(
                anchor="w"
            )
            ttk.Label(card, text=result.summary, wraplength=470).pack(
                anchor="w", pady=(3, 0)
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
            "This permanently deletes Azure Health Beacon's isolated Azure CLI profile and saved connection "
            "binding. Monitoring stops, but rules are retained. Your normal Azure CLI profile and Windows work "
            "account are not changed.",
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

    def _clear_page(self) -> None:
        for child in self.page.winfo_children():
            child.destroy()

    def _build_sign_in_page(self) -> None:
        self._clear_page()
        ttk.Label(
            self.page, text="Connect Azure Health Beacon", font=("Segoe UI", 18, "bold")
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
                "Your username, password, MFA response, and access tokens are handled only by Microsoft's sign-in "
                "flow and Azure CLI/WAM. Azure Health Beacon never receives or stores them. It stores only the "
                "tenant and subscription IDs you select. The app deletes its isolated Azure profile after 14 days."
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
        ttk.Label(
            self.page, text="Choose the Azure scope", font=("Segoe UI", 18, "bold")
        ).pack(anchor="w")
        ttk.Label(
            self.page,
            text="Select the subscription the Beacon should validate. This does not change Azure CLI's global subscription.",
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
        self.window.title("Manage Azure checks")
        self.window.geometry("760x470")
        self.window.minsize(680, 420)
        self.working = load_config()
        self.selected_id: str | None = None
        self.draft_id = str(uuid.uuid4())
        self.name = StringVar()
        self.reference = StringVar()
        self.tenant = StringVar()
        self.expected = StringVar(value="Succeeded")
        self.enabled = BooleanVar(value=True)
        self.last_tested_fingerprint: tuple[object, ...] | None = None
        self.test_status = StringVar(
            value="Paste an Azure Portal resource URL or resource ID."
        )
        self._build()
        for variable in (
            self.name,
            self.reference,
            self.tenant,
            self.expected,
            self.enabled,
        ):
            variable.trace_add("write", self._invalidate_test)
        self._refresh_list()

    def _build(self) -> None:
        outer = ttk.Frame(self.window, padding=14)
        outer.pack(fill=BOTH, expand=True)
        left = ttk.Frame(outer)
        left.pack(side=LEFT, fill=Y, padx=(0, 14))
        ttk.Label(left, text="Configured checks", font=("Segoe UI", 11, "bold")).pack(
            anchor="w"
        )
        self.listbox = __import__("tkinter").Listbox(left, width=30, height=16)
        self.listbox.pack(fill=Y, expand=True, pady=(8, 8))
        self.listbox.bind("<<ListboxSelect>>", self._select)
        ttk.Button(left, text="New check", command=self._new).pack(fill=X)
        ttk.Button(left, text="Remove selected", command=self._remove).pack(
            fill=X, pady=(6, 0)
        )
        ttk.Separator(left).pack(fill=X, pady=12)
        ttk.Button(left, text="Import rule pack…", command=self._import_rules).pack(
            fill=X
        )
        ttk.Button(left, text="Export all rules…", command=self._export_rules).pack(
            fill=X, pady=(6, 0)
        )

        form = ttk.Frame(outer)
        form.pack(side=LEFT, fill=BOTH, expand=True)
        ttk.Label(form, text="Check details", font=("Segoe UI", 11, "bold")).pack(
            anchor="w"
        )
        self._field(form, "Friendly name", self.name)
        self._field(form, "Azure Portal URL or resource ID", self.reference)
        self._field(form, "Tenant ID/domain (optional safety pin)", self.tenant)
        self._field(form, "Healthy provisioning states", self.expected)
        ttk.Checkbutton(form, text="Enabled", variable=self.enabled).pack(
            anchor="w", pady=(10, 0)
        )
        ttk.Label(form, textvariable=self.test_status, wraplength=430).pack(
            anchor="w", pady=(14, 8)
        )
        actions = ttk.Frame(form)
        actions.pack(fill=X, pady=(8, 0))
        ttk.Button(actions, text="Test without saving", command=self._test).pack(
            side=LEFT
        )
        self.apply_button = ttk.Button(
            actions, text="Apply tested rule", command=self._save_check
        )
        self.apply_button.pack(side=RIGHT)
        self.apply_button.state(["disabled"])
        ttk.Separator(form).pack(fill=X, pady=16)
        ttk.Label(
            form,
            text=f"Stored locally in {config_path()}\nNo Azure credentials or tokens are stored here.",
            wraplength=430,
        ).pack(anchor="w")

    @staticmethod
    def _field(parent: ttk.Frame, label: str, variable: StringVar) -> None:
        ttk.Label(parent, text=label).pack(anchor="w", pady=(10, 2))
        ttk.Entry(parent, textvariable=variable).pack(fill=X)

    def _refresh_list(self) -> None:
        self.listbox.delete(0, END)
        for check in self.working.checks:
            marker = "" if check.enabled else " (disabled)"
            self.listbox.insert(END, f"{check.name}{marker}")

    def _select(self, _event: object = None) -> None:
        selection = self.listbox.curselection()
        if not selection:
            return
        check = self.working.checks[selection[0]]
        self.selected_id = check.id
        self.name.set(check.name)
        self.reference.set(check.portal_url or check.resource_id)
        self.tenant.set(check.tenant_id)
        self.expected.set(", ".join(check.expected_values))
        self.enabled.set(check.enabled)
        self.test_status.set("Ready to test or edit.")

    def _new(self) -> None:
        self.selected_id = None
        self.draft_id = str(uuid.uuid4())
        self.name.set("")
        self.reference.set("")
        self.tenant.set("")
        self.expected.set("Succeeded")
        self.enabled.set(True)
        self.listbox.selection_clear(0, END)
        self.test_status.set("Paste an Azure Portal resource URL or resource ID.")

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
        )

    def _definition_from_form(self) -> CheckDefinition:
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
        return CheckDefinition(
            id=self.selected_id or self.draft_id,
            name=self.name.get().strip(),
            resource_id=resource_id,
            portal_url=portal_url,
            tenant_id=self.tenant.get().strip() or tenant_from_url or selected_tenant,
            expected_values=[
                item.strip() for item in self.expected.get().split(",") if item.strip()
            ],
            enabled=self.enabled.get(),
        )

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
            result = run_provisioning_check(
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
        self.test_status.set(f"{result.state.value.upper()}: {result.summary}")
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
            save_config(self.working)
        except Exception as error:
            messagebox.showerror("Could not save check", str(error), parent=self.window)
            return
        self.selected_id = definition.id
        self.test_status.set("Applied. A fresh background check has been queued.")
        self.last_tested_fingerprint = self._fingerprint(definition)
        self._refresh_list()
        self.app.reload_config()

    def _remove(self) -> None:
        selection = self.listbox.curselection()
        if not selection:
            return
        check = self.working.checks[selection[0]]
        if not messagebox.askyesno(
            "Remove check", f"Remove ‘{check.name}’?", parent=self.window
        ):
            return
        self.working.checks.pop(selection[0])
        save_config(self.working)
        self._new()
        self._refresh_list()
        self.app.reload_config()

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
            "The rule pack contains resource identifiers and expected states, but no credentials or tokens.",
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
            save_config(self.working)
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
        BeaconApp().run()
    except Exception:
        LOGGER.exception("Fatal application error")
        raise
    return 0


if __name__ == "__main__":
    sys.exit(main())
