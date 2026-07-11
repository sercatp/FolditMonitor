import os
import threading
from tkinter import messagebox

from foldit_speed_boost import (
    FolditSpeedBoostManager,
    SpeedBoostUnavailable,
    unavailable_message,
)


SPEED_BOOST_LABEL = "Enable speed boost"
STOP_SPEED_BOOST_LABEL = "Disable speed boost"
SPEED_BOOST_BUSY_LABEL = "Speed boost in progress..."
SPEED_BOOST_ALL_LABEL = "Enable speed boost for all clients"
STOP_SPEED_BOOST_ALL_LABEL = "Disable speed boost for all clients"


class FolditSpeedBoostIntegration:
    """Tk/Foldit Monitor glue for the optional Frida speed boost."""

    def __init__(self, root, process_tree, get_pid_tag):
        self.root = root
        self.process_tree = process_tree
        self.get_pid_tag = get_pid_tag
        self.manager = FolditSpeedBoostManager()
        self.armed_pids = set()
        self.script_running_pids = set()
        self.busy_pids = set()
        self.managed_pids = set()
        self.enabled_pids = set()
        self.client_names = {}
        self._lock = threading.RLock()
        self._closed = False
        self._pending_sync = None
        self._sync_running = False

    def remove_client_menu_items(self, menu) -> None:
        for i in range(menu.index("end"), -1, -1):
            try:
                label = menu.entrycget(i, "label")
            except Exception:
                continue
            if label in (SPEED_BOOST_LABEL, STOP_SPEED_BOOST_LABEL, SPEED_BOOST_BUSY_LABEL):
                menu.delete(i)

    def insert_client_menu_item(self, menu, insert_index: int, pid, folder: str) -> int:
        pid = int(pid)
        with self._lock:
            is_busy = pid in self.busy_pids
            is_armed = pid in self.armed_pids
        if is_busy:
            label = SPEED_BOOST_BUSY_LABEL
            state = "disabled"
        elif is_armed:
            label = STOP_SPEED_BOOST_LABEL
            state = "normal"
        else:
            label = SPEED_BOOST_LABEL
            state = "normal"
        menu.insert(
            insert_index,
            "command",
            label=label,
            state=state,
            command=lambda p=int(pid), c=os.path.basename(folder): self.toggle(p, c),
        )
        return insert_index + 1

    def before_activate(self, pid, after=None) -> bool:
        # This hook only shortens solver-worker waits, not the UI Sleep loop.
        # It is therefore safe to leave active while the Foldit window is open.
        return False

    def on_clients_refreshed(self, clients) -> None:
        sync_state = tuple(self._normalize_client_state(client) for client in clients)
        with self._lock:
            for pid, is_visible, client_name, script_running in sync_state:
                self.client_names[pid] = client_name
                if script_running is not None:
                    if script_running:
                        self.script_running_pids.add(pid)
                    else:
                        self.script_running_pids.discard(pid)
            self._pending_sync = sync_state
            if self._sync_running:
                return
            self._sync_running = True
        self._run_thread(self._worker_sync_clients)

    def on_script_finished(self, pid) -> None:
        pid = int(pid)
        with self._lock:
            self.script_running_pids.discard(pid)
            should_disable = pid in self.enabled_pids and pid not in self.busy_pids
            if should_disable:
                self.busy_pids.add(pid)
        if should_disable:
            self._run_thread(self._worker_disable_one, pid)

    def shutdown(self) -> None:
        self._closed = True
        self.manager.abandon_all()

    def add_global_menu_items(self, menu) -> None:
        menu.add_command(label=SPEED_BOOST_ALL_LABEL, command=self.speed_up_all)
        menu.add_command(label=STOP_SPEED_BOOST_ALL_LABEL, command=self.stop_all)

    def _show_dependency_message(self) -> None:
        messagebox.showinfo("Speed boost dependency", unavailable_message())

    def _row_clients(self):
        rows = []
        for item in self.process_tree.get_children():
            tags = self.process_tree.item(item, "tags")
            pid = self.get_pid_tag(tags)
            if pid is None:
                continue
            folder = next((tag for tag in tags if isinstance(tag, str) and ("\\" in tag or "/" in tag)), "")
            rows.append((int(pid), os.path.basename(folder), "active_window" in tags))
        return rows

    def _normalize_client_state(self, client):
        if isinstance(client, dict):
            return (
                int(client["pid"]),
                bool(client.get("is_window_visible", False)),
                str(client.get("client_name", "") or client.get("client", "") or client["pid"]),
                bool(client["script_running"]) if client.get("script_running") is not None else None,
            )
        return (
            int(client.pid),
            bool(client.is_window_visible),
            str(getattr(client, "client_name", "") or client.pid),
            None,
        )

    def _desired_enabled_locked(self, pid: int, _is_window_visible: bool) -> bool:
        return (
            int(pid) in self.armed_pids
            and int(pid) in self.script_running_pids
        )

    def toggle(self, pid, client_name: str = "") -> None:
        pid = int(pid)
        if not self.manager.is_supported():
            self._show_dependency_message()
            return
        with self._lock:
            if pid in self.busy_pids:
                return
            should_enable = pid not in self.armed_pids
            if should_enable:
                self.armed_pids.add(pid)
                self.client_names[pid] = client_name or self.client_names.get(pid, str(pid))
                enabled = self._desired_enabled_locked(pid, False)
                if not enabled:
                    return
            else:
                self.armed_pids.discard(pid)
                enabled = False
            self.busy_pids.add(pid)
        if should_enable:
            self._run_thread(self._worker_start_one, pid, client_name, enabled)
            return
        self._run_thread(self._worker_disable_one, pid)

    def speed_up_all(self) -> None:
        if not self.manager.is_supported():
            self._show_dependency_message()
            return
        rows = self._row_clients()
        for pid, client_name, is_visible in rows:
            with self._lock:
                self.armed_pids.add(pid)
                self.client_names[pid] = client_name or self.client_names.get(pid, str(pid))
                enabled = self._desired_enabled_locked(pid, is_visible)
                needs_worker = enabled or pid in self.enabled_pids
                if not needs_worker or not self._mark_busy_locked(pid):
                    continue
            if enabled:
                self._run_thread(self._worker_start_one, pid, client_name, True)
            else:
                self._run_thread(self._worker_disable_one, pid)

    def stop_all(self) -> None:
        with self._lock:
            pids = set(self.armed_pids)
        for pid in pids:
            with self._lock:
                if not self._mark_busy_locked(pid):
                    continue
                self.armed_pids.discard(pid)
            self._run_thread(self._worker_disable_one, pid)

    def _after(self, delay_ms: int, callback) -> None:
        if self._closed:
            return
        try:
            self.root.after(delay_ms, callback)
        except Exception:
            pass

    def _run_thread(self, target, *args) -> None:
        if self._closed:
            return
        threading.Thread(target=target, args=args, daemon=True).start()

    def _mark_busy(self, pid: int) -> bool:
        with self._lock:
            return self._mark_busy_locked(pid)

    def _mark_busy_locked(self, pid: int) -> bool:
        pid = int(pid)
        if pid in self.busy_pids:
            return False
        self.busy_pids.add(pid)
        return True

    def _refresh_snapshot(self) -> None:
        snapshot = self.manager.snapshot()
        with self._lock:
            self.managed_pids = set(snapshot)
            self.enabled_pids = {pid for pid, enabled in snapshot.items() if enabled}

    def _clear_busy(self, pids) -> None:
        with self._lock:
            self.busy_pids.difference_update(int(pid) for pid in pids)

    def _post_error(self, message: str) -> None:
        self._after(0, lambda: messagebox.showerror("Speed boost failed", message))

    def _worker_start_one(self, pid: int, client_name: str, enabled: bool) -> None:
        try:
            self.manager.start(pid, client_name=client_name, enabled=enabled)
            if enabled:
                with self._lock:
                    still_should_enable = (
                        pid in self.armed_pids
                        and pid in self.script_running_pids
                    )
                if not still_should_enable:
                    self.manager.disable(pid)
            self._refresh_snapshot()
        except SpeedBoostUnavailable:
            with self._lock:
                self.armed_pids.discard(pid)
            self._after(0, self._show_dependency_message)
        except Exception as exc:
            with self._lock:
                self.armed_pids.discard(pid)
            self.manager.log(f"Speed boost pid={pid}: start failed: {exc}")
        finally:
            self._clear_busy((pid,))

    def _worker_enable_one(self, pid: int, client_name: str) -> None:
        try:
            self.manager.start(pid, client_name=client_name, enabled=True)
            with self._lock:
                still_should_enable = (
                    pid in self.armed_pids
                    and pid in self.script_running_pids
                )
            if not still_should_enable:
                self.manager.disable(pid)
            self._refresh_snapshot()
        except SpeedBoostUnavailable:
            self._after(0, self._show_dependency_message)
        except Exception as exc:
            self.manager.log(f"Speed boost pid={pid}: enable failed: {exc}")
        finally:
            self._clear_busy((pid,))

    def _worker_disable_one(self, pid: int) -> None:
        try:
            self.manager.disable(pid)
            self._refresh_snapshot()
        except Exception as exc:
            self.manager.log(f"Speed boost pid={pid}: disable failed: {exc}")
        finally:
            self._clear_busy((pid,))

    def _worker_sync_clients(self) -> None:
        while not self._closed:
            with self._lock:
                sync_state = self._pending_sync
                self._pending_sync = None
            if sync_state is None:
                with self._lock:
                    self._sync_running = False
                return

            live_pids = {pid for pid, _, _, _ in sync_state}
            visible_by_pid = {pid: is_visible for pid, is_visible, _, _ in sync_state}
            client_name_by_pid = {pid: client_name for pid, _, client_name, _ in sync_state}
            snapshot = self.manager.snapshot()
            for pid in list(snapshot):
                if pid not in live_pids:
                    if self._mark_busy(pid):
                        try:
                            self.manager.forget(pid)
                            with self._lock:
                                self.armed_pids.discard(pid)
                                self.script_running_pids.discard(pid)
                                self.client_names.pop(pid, None)
                            self._refresh_snapshot()
                        finally:
                            self._clear_busy((pid,))
            for pid, is_visible in visible_by_pid.items():
                with self._lock:
                    desired_enabled = self._desired_enabled_locked(pid, is_visible)
                    client_name = client_name_by_pid.get(pid) or self.client_names.get(pid, str(pid))
                    current_enabled = snapshot.get(pid)
                if current_enabled is None:
                    if not desired_enabled:
                        continue
                    if self._mark_busy(pid):
                        self._run_thread(self._worker_enable_one, pid, client_name)
                    continue
                if current_enabled == desired_enabled:
                    continue
                if self._mark_busy(pid):
                    try:
                        self.manager.set_enabled(pid, desired_enabled)
                        self._refresh_snapshot()
                    finally:
                        self._clear_busy((pid,))

            with self._lock:
                if self._pending_sync is None:
                    self._sync_running = False
                    break
