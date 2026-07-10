import os
import platform
import subprocess
import time
from dataclasses import dataclass
from typing import Any, Optional

import psutil


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
    process: psutil.Process
    window_info: Any = None
    window_title: str = ""
    is_window_visible: bool = False

class WindowManager:
    def __init__(self):
        self.system = platform.system()
        # Import all required modules at class initialization
        if self.system == 'Linux':
            try:
                import Xlib.display
                import Xlib.X
                self.Xlib = Xlib
                self.display = Xlib.display.Display()
            except ImportError:
                print("For Linux, python-xlib is required. Install: pip install python-xlib")
                self.Xlib = None
                self.display = None
        elif self.system == 'Darwin':  # MacOS
            try:
                import AppKit
                self.AppKit = AppKit
                self.workspace = AppKit.NSWorkspace.sharedWorkspace()
            except ImportError:
                print("For macOS, pyobjc-framework-Cocoa is required. Install: pip install pyobjc-framework-Cocoa")
                self.AppKit = None
                self.workspace = None
        else:  # Windows
            try:
                import win32gui
                import win32process
                import win32con
                self.win32gui = win32gui
                self.win32process = win32process
                self.win32con = win32con
            except ImportError:
                print("For Windows, pywin32 is required. Install: pip install pywin32")
                self.win32gui = None
                self.win32process = None
                self.win32con = None

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
        windows = []
        def enum_callback(hwnd, results):
            if self.win32gui.IsWindowVisible(hwnd):
                _, found_pid = self.win32process.GetWindowThreadProcessId(hwnd)
                if found_pid == pid:
                    title = self.win32gui.GetWindowText(hwnd)
                    class_name = self.win32gui.GetClassName(hwnd)
                    windows.append((hwnd, title, class_name))
        self.win32gui.EnumWindows(enum_callback, None)
        return windows

    def _get_linux_process_windows(self, pid):
        if not self.Xlib or not self.display:
            return []
        windows = []
        root = self.display.screen().root
        window_ids = root.get_full_property(
            self.display.intern_atom('_NET_CLIENT_LIST'), 
            self.Xlib.X.AnyPropertyType
        ).value
        
        for window_id in window_ids:
            window = self.display.create_resource_object('window', window_id)
            window_pid = window.get_full_property(
                self.display.intern_atom('_NET_WM_PID'), 
                self.Xlib.X.AnyPropertyType
            )
            
            if window_pid and window_pid.value[0] == pid:
                title = window.get_wm_name()
                class_name = window.get_wm_class()
                windows.append((window_id, title, class_name))
        return windows

    def _get_macos_process_windows(self, pid):
        if not self.AppKit:
            return []
        windows = []
        for window in self.AppKit.NSApp.windows():
            if window.processIdentifier() == pid:
                title = window.title()
                windows.append((window, title, ""))
        return windows

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
            window = window_info[0]
            window.makeKeyAndOrderFront_(None)

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
            if not self.AppKit:
                return False
            window = window_info[0]
            return window.isVisible() and not window.isMiniaturized()
        
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

    def list_foldit_clients(self, executable_name: Optional[str] = None) -> list[ClientInfo]:
        expected_name = executable_name or self.get_executable_name()
        if not expected_name:
            return []

        clients = []
        for proc in psutil.process_iter(["pid", "name", "exe"]):
            try:
                if not self._is_foldit_process(proc, expected_name):
                    continue

                executable_path = self._get_process_executable_path(proc)
                folder = os.path.dirname(executable_path) if executable_path else ""
                client_name = os.path.basename(folder) if folder else str(proc.pid)

                selected_window = None
                selected_title = ""
                is_visible = False
                windows = self.get_process_windows(proc.pid)
                foldit_windows = [
                    window
                    for window in windows
                    if "Foldit" in str(window[1] or "")
                ]
                if foldit_windows:
                    selected_window = foldit_windows[0]
                    selected_title = str(selected_window[1] or "")
                    is_visible = self.is_window_visible(selected_window)

                clients.append(
                    ClientInfo(
                        pid=proc.pid,
                        folder=folder,
                        client_name=client_name,
                        executable_path=executable_path,
                        process=proc,
                        window_info=selected_window,
                        window_title=selected_title,
                        is_window_visible=is_visible,
                    )
                )
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess, OSError):
                continue
        return clients

    def activate_client(self, pid: int) -> bool:
        windows = self.get_process_windows(int(pid))
        foldit_windows = [
            window
            for window in windows
            if "Foldit" in str(window[1] or "")
        ]
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
                executable_path = os.path.join(folder_path, expected_name)
                if os.path.isdir(folder_path) and os.path.exists(executable_path) and folder_path not in found_folders:
                    found_folders.append(folder_path)
        return found_folders

    def launch_client(self, folder_path: str, executable_name: Optional[str] = None):
        expected_name = executable_name or self.get_executable_name()
        if not expected_name:
            raise OSError(f"Unsupported platform: {self.system}")

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

    def open_client_load_dialog(self, pid: int) -> bool:
        return self.send_client_shortcut(pid, "ctrl+o")

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
    """Open the folder in the default file explorer."""
    open_path(folder_path)


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
