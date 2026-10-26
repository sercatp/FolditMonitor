import ctypes
import os
import platform
import subprocess
import time
from dataclasses import dataclass
from typing import Any, Optional

import psutil


def enable_native_dpi_awareness() -> bool:
    """Enable native monitor scaling for a Windows GUI process.

    This must run before the first Tk window is created. Other operating
    systems keep their existing toolkit-managed scaling unchanged.
    """
    if platform.system() != "Windows":
        return False

    try:
        set_context = ctypes.windll.user32.SetProcessDpiAwarenessContext
        set_context.argtypes = [ctypes.c_void_p]
        set_context.restype = ctypes.c_bool
        if set_context(ctypes.c_void_p(-4)):  # PER_MONITOR_AWARE_V2
            return True
    except (AttributeError, OSError):
        pass

    try:
        result = ctypes.windll.shcore.SetProcessDpiAwareness(2)  # Per-monitor aware
        if result == 0:
            return True
    except (AttributeError, OSError):
        pass

    try:
        return bool(ctypes.windll.user32.SetProcessDPIAware())
    except (AttributeError, OSError):
        return False


FOLDIT_EXECUTABLES = {
    "Windows": "Foldit.exe",
    "Linux": "Foldit",
    "Darwin": "Foldit.app",
}


@dataclass
class ClientInfo:
    pid: int
    folder: str
    client_name: str
    executable_path: str
    installation_path: str
    data_root: str
    process_start_time: float
    process: psutil.Process
    window_info: Any = None
    window_title: str = ""
    window_title_available: bool = False
    is_window_visible: bool = False
    is_window_focused: bool = False

class WindowManager:
    def __init__(self):
        self.system = platform.system()
        self._windows_by_pid = {}
        self._linux_by_pid = {}
        self._mac_windows_by_pid = {}
        # Import all required modules at class initialization
        if self.system == 'Linux':
            try:
                import Xlib.display
                import Xlib.X
                self.Xlib = Xlib
                self.display = Xlib.display.Display()
                self._linux_by_pid = {}
            except ImportError:
                print("For Linux, python-xlib is required. Install: pip install python-xlib")
                self.Xlib = None
                self.display = None
                self._linux_by_pid = {}
        elif self.system == 'Darwin':  # MacOS
            try:
                import AppKit
                import Quartz
                self.AppKit = AppKit
                self.Quartz = Quartz
                self.workspace = AppKit.NSWorkspace.sharedWorkspace()
                self._mac_windows_by_pid = {}
            except ImportError:
                print(
                    "For macOS, pyobjc-framework-Cocoa and pyobjc-framework-Quartz are required. "
                    "Install: pip install pyobjc-framework-Cocoa pyobjc-framework-Quartz"
                )
                self.AppKit = None
                self.Quartz = None
                self.workspace = None
                self._mac_windows_by_pid = {}
        else:  # Windows
            try:
                import win32gui
                import win32process
                import win32con
                self.win32gui = win32gui
                self.win32process = win32process
                self.win32con = win32con
                self._windows_by_pid = {}
            except ImportError:
                print("For Windows, pywin32 is required. Install: pip install pywin32")
                self.win32gui = None
                self.win32process = None
                self.win32con = None
                self._windows_by_pid = {}

    def get_process_windows(self, pid):
        if self.system == 'Windows':
            return self._get_windows_process_windows(pid)
        elif self.system == 'Linux':
            return self._get_linux_process_windows(pid)
        elif self.system == 'Darwin':
            return self._get_macos_process_windows(pid)
        return []

    def _get_windows_process_windows(self, pid):
        if not all([self.win32gui, self.win32process]):
            return []
        return list(self._windows_by_pid.get(int(pid), ()))

    def _refresh_windows_window_cache(self):
        if not all([self.win32gui, self.win32process]):
            self._windows_by_pid = {}
            return
        grouped = {}

        def enum_callback(hwnd, results):
            try:
                if not self.win32gui.IsWindowVisible(hwnd):
                    return
                _, found_pid = self.win32process.GetWindowThreadProcessId(hwnd)
                title = self.win32gui.GetWindowText(hwnd)
                class_name = self.win32gui.GetClassName(hwnd)
                grouped.setdefault(int(found_pid), []).append((hwnd, title, class_name))
            except Exception:
                return

        self.win32gui.EnumWindows(enum_callback, None)
        self._windows_by_pid = grouped

    def _get_linux_process_windows(self, pid):
        if not self.Xlib or not self.display:
            return []
        return list(getattr(self, "_linux_by_pid", {}).get(int(pid), ()))

    def _refresh_linux_window_cache(self):
        if not self.Xlib or not self.display:
            self._linux_by_pid = {}
            return
        grouped = {}
        root = self.display.screen().root
        try:
            client_list = root.get_full_property(
                self.display.intern_atom('_NET_CLIENT_LIST'),
                self.Xlib.X.AnyPropertyType,
            )
            window_ids = client_list.value if client_list is not None else ()
        except Exception:
            self._linux_by_pid = {}
            return

        for window_id in window_ids:
            try:
                window = self.display.create_resource_object('window', window_id)
                window_pid = window.get_full_property(
                    self.display.intern_atom('_NET_WM_PID'),
                    self.Xlib.X.AnyPropertyType,
                )
                if not window_pid:
                    continue
                pid = int(window_pid.value[0])
                title = window.get_wm_name()
                class_name = window.get_wm_class()
                grouped.setdefault(pid, []).append((window_id, title, class_name))
            except Exception:
                continue
        self._linux_by_pid = grouped

    def _get_macos_process_windows(self, pid):
        if not self.AppKit or not self.Quartz:
            return []
        return list(self._mac_windows_by_pid.get(int(pid), ()))

    def _refresh_macos_window_cache(self):
        if not self.AppKit or not self.Quartz:
            self._mac_windows_by_pid = {}
            return
        try:
            raw_windows = self.Quartz.CGWindowListCopyWindowInfo(
                self.Quartz.kCGWindowListOptionAll,
                self.Quartz.kCGNullWindowID,
            ) or []
        except Exception:
            self._mac_windows_by_pid = {}
            return

        grouped = {}
        for metadata in raw_windows:
            try:
                pid = int(metadata.get(self.Quartz.kCGWindowOwnerPID, 0) or 0)
            except (TypeError, ValueError):
                continue
            if pid <= 0:
                continue
            title = str(metadata.get(self.Quartz.kCGWindowName, "") or "")
            owner_name = str(metadata.get(self.Quartz.kCGWindowOwnerName, "") or "")
            window_id = int(metadata.get(self.Quartz.kCGWindowNumber, 0) or 0)
            grouped.setdefault(pid, []).append((window_id, title, owner_name, dict(metadata)))

        def priority(window):
            metadata = window[3]
            layer = int(metadata.get(self.Quartz.kCGWindowLayer, 0) or 0)
            onscreen = bool(metadata.get(self.Quartz.kCGWindowIsOnscreen, False))
            bounds = metadata.get(self.Quartz.kCGWindowBounds, {}) or {}
            try:
                area = float(bounds.get("Width", 0) or 0) * float(bounds.get("Height", 0) or 0)
            except (TypeError, ValueError):
                area = 0.0
            return (layer != 0, not onscreen, -area)

        for windows in grouped.values():
            windows.sort(key=priority)
        self._mac_windows_by_pid = grouped

    def activate_window(self, window_info):
        if self.system == 'Windows':
            if not all([self.win32gui, self.win32con]):
                return
            hwnd = window_info[0]
            if self.win32gui.IsIconic(hwnd):
                self.win32gui.SendMessage(hwnd, self.win32con.WM_SYSCOMMAND, 
                                        self.win32con.SC_RESTORE, 0)
            self.win32gui.SetForegroundWindow(hwnd)
        
        elif self.system == 'Linux':
            if not self.Xlib or not self.display:
                return
            window_id = window_info[0]
            window = self.display.create_resource_object('window', window_id)
            window.set_input_focus(self.Xlib.X.RevertToParent, 
                                 self.Xlib.X.CurrentTime)
            window.configure(stack_mode=self.Xlib.X.Above)
            self.display.sync()
        
        elif self.system == 'Darwin':
            if not self.AppKit:
                return
            metadata = window_info[3] if len(window_info) > 3 else {}
            try:
                pid = int(metadata.get(self.Quartz.kCGWindowOwnerPID, 0) or 0)
            except (TypeError, ValueError, AttributeError):
                return
            application = self.AppKit.NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
            if application is not None:
                application.activateWithOptions_(self.AppKit.NSApplicationActivateAllWindows)

    def is_window_visible(self, window_info):
        """Check if the window is visible and not minimized"""
        if self.system == 'Windows':
            if not self.win32gui:
                return False
            hwnd = window_info[0]
            return (self.win32gui.IsWindowVisible(hwnd) and 
                   not self.win32gui.IsIconic(hwnd))
        
        elif self.system == 'Linux':
            if not self.Xlib or not self.display:
                return False
            window_id = window_info[0]
            window = self.display.create_resource_object('window', window_id)
            return window.get_attributes().map_state == self.Xlib.X.IsViewable
        
        elif self.system == 'Darwin':
            if not self.AppKit or not self.Quartz:
                return False
            metadata = window_info[3] if len(window_info) > 3 else {}
            return bool(metadata.get(self.Quartz.kCGWindowIsOnscreen, False))
        
        return False

    def is_window_focused(self, window_info):
        """Check whether this Foldit window owns the current keyboard focus."""
        if not window_info:
            return False

        if self.system == 'Windows':
            if not self.win32gui:
                return False
            return self.win32gui.GetForegroundWindow() == window_info[0]

        if self.system == 'Linux':
            if not self.Xlib or not self.display:
                return False
            try:
                active_window = self.display.screen().root.get_full_property(
                    self.display.intern_atom('_NET_ACTIVE_WINDOW'),
                    self.Xlib.X.AnyPropertyType,
                )
                return bool(active_window and active_window.value[0] == window_info[0])
            except Exception:
                return False

        if self.system == 'Darwin':
            if not self.AppKit or not self.Quartz:
                return False
            metadata = window_info[3] if len(window_info) > 3 else {}
            try:
                pid = int(metadata.get(self.Quartz.kCGWindowOwnerPID, 0) or 0)
            except (TypeError, ValueError, AttributeError):
                return False
            application = self.AppKit.NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
            return bool(application and application.isActive())

        return False

    def get_executable_name(self) -> Optional[str]:
        return FOLDIT_EXECUTABLES.get(self.system)

    def _get_process_executable_path(self, proc: psutil.Process) -> str:
        exe_path = proc.info.get("exe") if hasattr(proc, "info") else None
        if exe_path:
            return str(exe_path)
        try:
            return str(proc.exe())
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess, OSError):
            return ""

    def _get_process_name_candidates(self, proc: psutil.Process) -> set[str]:
        candidates = set()
        info_name = proc.info.get("name") if hasattr(proc, "info") else None
        if info_name:
            candidates.add(os.path.basename(str(info_name)).strip().lower())

        exe_path = self._get_process_executable_path(proc)
        if exe_path:
            candidates.add(os.path.basename(exe_path).strip().lower())

        normalized = set()
        for candidate in candidates:
            if not candidate:
                continue
            normalized.add(candidate)
            root, _ = os.path.splitext(candidate)
            if root:
                normalized.add(root)
            if candidate.endswith(".app"):
                normalized.add(candidate[:-4])
        return normalized

    def _is_foldit_process(self, proc: psutil.Process, executable_name: str) -> bool:
        expected = os.path.basename(str(executable_name or "")).strip().lower()
        if not expected:
            return False

        expected_names = {expected}
        root, _ = os.path.splitext(expected)
        if root:
            expected_names.add(root)
        if expected.endswith(".app"):
            expected_names.add(expected[:-4])

        return bool(self._get_process_name_candidates(proc) & expected_names)

    def resolve_client_layout(self, executable_path: str) -> tuple[str, str, str]:
        """Return (installation path, data root, display name) for a Foldit executable."""
        clean_executable = os.path.realpath(os.path.abspath(str(executable_path or "")))
        if self.system == "Darwin" and clean_executable:
            current = os.path.dirname(clean_executable)
            while current and os.path.dirname(current) != current:
                if current.casefold().endswith(".app"):
                    installation_path = current
                    data_root = os.path.join(current, "Contents", "Resources")
                    app_name = os.path.splitext(os.path.basename(current))[0]
                    return (
                        os.path.realpath(installation_path),
                        os.path.realpath(data_root),
                        app_name or "Foldit",
                    )
                current = os.path.dirname(current)

        data_root = os.path.dirname(clean_executable) if clean_executable else ""
        installation_path = data_root
        return installation_path, data_root, os.path.basename(data_root) if data_root else ""

    def _is_foldit_window(self, window: Any) -> bool:
        title = str(window[1] or "") if len(window) > 1 else ""
        class_name = window[2] if len(window) > 2 else ""
        if isinstance(class_name, (tuple, list)):
            class_text = " ".join(str(part or "") for part in class_name)
        else:
            class_text = str(class_name or "")
        if self.system == "Darwin":
            return True
        if self.system == "Windows" and class_text.casefold() == "foldit":
            return True
        return "foldit" in f"{title} {class_text}".casefold()

    def list_foldit_clients(self, executable_name: Optional[str] = None) -> list[ClientInfo]:
        expected_name = executable_name or self.get_executable_name()
        if not expected_name:
            return []

        if self.system == "Windows":
            self._refresh_windows_window_cache()
        elif self.system == "Linux":
            self._refresh_linux_window_cache()
        elif self.system == "Darwin":
            self._refresh_macos_window_cache()

        clients = []
        for proc in psutil.process_iter(["pid", "name", "exe"]):
            try:
                if not self._is_foldit_process(proc, expected_name):
                    continue

                executable_path = self._get_process_executable_path(proc)
                installation_path, data_root, client_name = self.resolve_client_layout(executable_path)
                folder = data_root
                try:
                    process_start_time = float(proc.create_time())
                except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess, OSError):
                    process_start_time = 0.0

                selected_window = None
                selected_title = ""
                title_available = False
                is_visible = False
                is_focused = False
                windows = self.get_process_windows(proc.pid)
                foldit_windows = [window for window in windows if self._is_foldit_window(window)]
                if foldit_windows:
                    selected_window = foldit_windows[0]
                    selected_title = str(selected_window[1] or "")
                    title_available = bool(selected_title.strip())
                    is_visible = self.is_window_visible(selected_window)
                    is_focused = self.is_window_focused(selected_window)

                clients.append(
                    ClientInfo(
                        pid=proc.pid,
                        folder=folder,
                        client_name=client_name,
                        executable_path=executable_path,
                        installation_path=installation_path,
                        data_root=data_root,
                        process_start_time=process_start_time,
                        process=proc,
                        window_info=selected_window,
                        window_title=selected_title,
                        window_title_available=title_available,
                        is_window_visible=is_visible,
                        is_window_focused=is_focused,
                    )
                )
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess, OSError):
                continue
        return clients

    def activate_client(self, pid: int) -> bool:
        if self.system == "Darwin":
            if not self.AppKit:
                return False
            application = self.AppKit.NSRunningApplication.runningApplicationWithProcessIdentifier_(int(pid))
            if application is None:
                return False
            return bool(application.activateWithOptions_(self.AppKit.NSApplicationActivateAllWindows))

        if self.system == "Windows":
            self._refresh_windows_window_cache()
        elif self.system == "Linux":
            self._refresh_linux_window_cache()
        windows = self.get_process_windows(int(pid))
        foldit_windows = [window for window in windows if self._is_foldit_window(window)]
        if not foldit_windows:
            return False
        self.activate_window(foldit_windows[0])
        return True

    def find_installation_folders(self, parent_dirs: list[str], executable_name: Optional[str] = None) -> list[str]:
        expected_name = executable_name or self.get_executable_name()
        if not expected_name:
            return []

        found_folders = []
        for parent_dir in parent_dirs:
            try:
                entries = os.listdir(parent_dir)
            except OSError:
                continue

            for entry in entries:
                folder_path = os.path.join(parent_dir, entry)
                if self.system == "Darwin":
                    if not entry.casefold().endswith(".app") or not os.path.isdir(folder_path):
                        continue
                    executable_path = os.path.join(folder_path, "Contents", "MacOS", "Foldit")
                    data_root = os.path.join(folder_path, "Contents", "Resources")
                    if os.path.isfile(executable_path) and data_root not in found_folders:
                        found_folders.append(os.path.realpath(data_root))
                    continue
                executable_path = os.path.join(folder_path, expected_name)
                if os.path.isdir(folder_path) and os.path.exists(executable_path) and folder_path not in found_folders:
                    found_folders.append(folder_path)
        return found_folders

    def launch_client(self, folder_path: str, executable_name: Optional[str] = None):
        expected_name = executable_name or self.get_executable_name()
        if not expected_name:
            raise OSError(f"Unsupported platform: {self.system}")

        if self.system == "Darwin":
            clean_path = os.path.realpath(os.path.abspath(folder_path))
            app_path = clean_path
            while app_path and not app_path.casefold().endswith(".app"):
                parent = os.path.dirname(app_path)
                if parent == app_path:
                    break
                app_path = parent
            if not app_path.casefold().endswith(".app"):
                raise FileNotFoundError(f"Foldit.app was not found above {folder_path}")
            subprocess.Popen(["/usr/bin/open", "-n", app_path])
            return app_path

        executable_path = os.path.join(folder_path, expected_name)
        if self.system == "Windows":
            os.startfile(executable_path)
        else:
            subprocess.Popen([executable_path])
        return executable_path

    def send_client_shortcut(self, pid: int, shortcut: str) -> bool:
        if not self.activate_client(pid):
            return False
        if self.system != "Windows":
            return False

        clean_shortcut = str(shortcut).strip().lower()
        if not clean_shortcut:
            return False

        time.sleep(0.2)
        try:
            import keyboard

            keyboard.press(clean_shortcut)
            keyboard.release(clean_shortcut)
        except Exception as exc:
            print(f"Error pressing {clean_shortcut}: {exc}")
        return True

def open_path(path):
    """Open a file or folder with the system default application."""
    system = platform.system()
    if system == 'Darwin':  # macOS
        subprocess.Popen(["open", path])
    elif system == 'Windows':
        os.startfile(path)
    elif system == 'Linux':
        subprocess.Popen(["xdg-open", path])
    else:
        raise OSError(f"Unsupported platform: {system}")


def open_folder(folder_path):
    """Open a folder in the platform's default file manager."""
    open_path(os.path.abspath(folder_path))


def open_containing_folder(path):
    """Open the directory containing *path* in the default file manager."""
    absolute_path = os.path.abspath(path)
    folder_path = absolute_path if os.path.isdir(absolute_path) else os.path.dirname(absolute_path)
    open_folder(folder_path)


def delete_file(path):
    """Permanently delete one file on Windows, macOS, or Linux."""
    os.unlink(os.path.abspath(path))


def open_file(path, reveal_end=False):
    """Open a file and optionally send a Windows-only navigation shortcut."""
    open_path(path)
    if not reveal_end or platform.system() != "Windows":
        return

    time.sleep(0.3)
    try:
        import keyboard

        keyboard.press("ctrl+end")
        keyboard.release("ctrl+end")
    except Exception as exc:
        print(f"Error pressing ctrl+end: {exc}")

#----------------------------------------------------------------------------------------------------------- MEDIA FUNCTIONS
def create_ribbon_icon(width=16, height=16):
    """Create icon with color #6b953b and black dots grid"""
    import math
    from PIL import Image, ImageDraw
    import io
    
    # Create a blank image with transparent background
    img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Define the sine wave parameters
    num_lines = 10
    x_values = [x for x in range(width)]
    
    def sine_wave(x):
        return (height / 4) + (height / 4) * math.sin(2 * math.pi * (x / width))

    # Draw the ribbon by creating filled rectangles
    for i in range(num_lines):
        y1 = sine_wave(x_values[0]) + i * 0.5        
        y2 = y1 + 2
        for x in x_values:
            y1 = sine_wave(x) + i * 0.5
            y2 = y1 + 2
            # Draw a line with a gradual blue color
            color = (0, int(255 * (i / num_lines)), int(255 * (1 - i / num_lines)), 255)
            draw.line([x, y1, x, y2], fill=color)

    # Save image to bytes buffer instead of file
    img_buffer = io.BytesIO()
    img.save(img_buffer, format='PNG')
    img_buffer.seek(0)
    
    return img_buffer.getvalue()

def create_alert_sound(duration=2, sample_rate=44100, volume=0.3):
    """Create sound signal from three notes, which start at 0s, 0.05s, 0s"""
    import numpy as np
    import io
    import wave as wave_module
    
    t = np.linspace(0, duration, int(sample_rate * duration))
    freqs = [622.25, 466.16, 1864.66]  # D#5, A#4, A#5
    
    #Create envelope for sounds with decay.
    def envelope(t, start_time, duration=2):
        env = np.zeros_like(t)
        start_idx, attack_samples = int(start_time * sample_rate), int(0.09 * sample_rate)
        t2, t3 = min(start_idx + attack_samples, len(t)), min(int(duration * sample_rate), len(t))
        if start_idx < t2: env[start_idx:t2] = np.linspace(0, 1, t2 - start_idx)
        if t2 < t3: env[t2:t3] = np.exp(-3 * np.linspace(0, 3, t3 - t2))
        return env
    
    scale = duration / 0.5
    waves = [np.sin(2 * np.pi * f * t) * envelope(t, start * scale) for start, f in zip([0.0, 0.03, 0.0], freqs)]
    waves[2] *= 0.1  # Reduce volume of third sound
    wave = np.mean(waves, axis=0)
    
    wave = np.tanh(wave * 1.5) * 0.8  # Add non-linearity
    wave = np.int16(wave * 32767 * volume)  # Convert to 16-bit
    stereo = np.column_stack((wave, wave))
    
    # Convert numpy array to bytes buffer
    buffer = io.BytesIO()
    with wave_module.open(buffer, 'wb') as wave_file:
        wave_file.setnchannels(2)
        wave_file.setsampwidth(2)
        wave_file.setframerate(sample_rate)
        wave_file.writeframes(stereo.tobytes())
    
    buffer.seek(0)
    return buffer 
