# Foldit Client Manager from Serca v1.0

# Large piece of monkey code
import psutil
import time
import os
# pygame is imported lazily inside the sound helpers below: its SDL init costs ~1s,
# and it is only needed when an alarm actually plays, never on the startup path.
from collections import defaultdict, deque
import tkinter as tk
from tkinter import ttk, font, messagebox
import threading
import re

import datetime
import shutil
import tempfile

PUZZLE_ID_RE = re.compile(r"""
    (?<!\d)                     # Do not start inside a longer number.
    (?P<puzzle>\d{3,}[A-Za-z]*) # 3+ digits with an optional Latin suffix.
    (?=$|[^A-Za-z])             # Stop at the end or before a non-Latin char.
""", re.VERBOSE)

from network import (
    ARTIFACT_QUERY_CAPABILITY,
    ARTIFACT_TRANSFER_CAPABILITY,
    NetworkManager,
    ConnectDialog,
    RemoteTreeView,
    sanitize_artifact_filename,
)
from log_lookup import find_matching_log_file, values_match_log_query
from window_manager import WindowManager, open_folder, open_file, create_ribbon_icon
from settings import Settings
from tooltip import TooltipWindow
from logger import FolditLogHandler
from foldit_speed_boost_integration import FolditSpeedBoostIntegration
from stats_module import StatsManager, parse_numeric_score
from stats_ui import (
    close_stats_window_if_exists,
    get_open_stats_window,
    is_stats_window_user_interacting,
    show_stats,
)
try:
    from savefile_api import export_pdb, get_basic_info
except Exception as e:
    export_pdb = None
    get_basic_info = None
    print(f"savefile_api unavailable: {e}")

# Define the root path and settings manager
root_path = os.path.dirname(os.path.abspath(__file__))
settings_manager = Settings(root_path)
stats_manager = StatsManager(root_path, settings_manager.settings)

# Create a dictionary with the necessary settings for log processing
foldit_log_settings = {
    'MAX_LINES': settings_manager.settings['logging']['max_lines'],
    'EXCLUSION_CRITERIA': settings_manager.settings['logging']['exclude_score_strings'],
    'SCRIPT_TYPE_MAPPING': settings_manager.settings['script_type_mapping'],
    'SCRIPT_TYPE_FALLBACK_MAX_LENGTH': settings_manager.settings['display'].get('script_type_fallback_max_length', 10),
    'CHECK_INTERVAL': settings_manager.CHECK_INTERVAL,
    'SCORE_PATTERNS': settings_manager.SCORE_PATTERNS,
    'tooltip_lines': settings_manager.settings['display']['tooltip_lines'],
    'max_line_length': settings_manager.settings['logging']['max_line_length'],
    'managed_log_exports': settings_manager.settings['logging'].get('managed_log_exports', True),
}
# Create an instance with the correct settings
foldit_log_handler = FolditLogHandler(foldit_log_settings)
speed_boost = None

# Now use settings_manager instead of settings
window_width = 360
window_height = 55

# Get the window position from the settings
settings_x = settings_manager.settings['display']['window_position']['x']
settings_y = settings_manager.settings['display']['window_position']['y']


def is_ui_interacting():
    return (
        ('root' in globals() and (getattr(root, 'is_dragging', False) or getattr(root, 'resizing', False)))
        or is_stats_window_user_interacting()
    )

normal_font = None
bold_font = None 
italic_font = None
tooltip_font = None  
def init_fonts():
    global normal_font, bold_font, italic_font, tooltip_font
    family = settings_manager.settings['display']['fonts']['family']
    size = settings_manager.settings['display']['fonts']['normal_size']
    tooltip_size = settings_manager.settings['display']['fonts']['tooltip_size']
    
    normal_font = font.Font(family=family, size=size)
    bold_font = font.Font(family=family, size=size, weight="bold")
    italic_font = font.Font(family=family, size=size, slant="italic")
    tooltip_font = font.Font(family=family, size=tooltip_size)

backup_folder_name = "foldit_backup"
save2backup = True  # Whether to create a backup for destination folder when copying saves

# Process monitoring dictionary
monitored_processes = defaultdict(lambda: {
    'cpu_history': deque(maxlen=settings_manager.MONITOR_DURATION // settings_manager.CHECK_INTERVAL), 
    'high_cpu_count': 0, 
    'low_cpu_count': 0, 
    'low_cpu_start': None,
    'last_log_lines': deque(maxlen=settings_manager.tooltip_lines),  # Storing the last 15 (tooltip_lines) lines
    'last_log_update': None,  # Time of the last log update
    'puzzle_number': None,
    'last_score_value': None,
    'last_script_change_token': 0,
    'score_stale_ticks': 0,
    'was_idle': False,
    'alarm_on_change': False,  # one-shot: beep when score changes, then auto-disarm
})

selected_rows = []  # Array for storing selected rows
artifact_row_cache = {}
artifact_row_cache_lock = threading.Lock()
client_log_roots_by_client = defaultdict(set)
last_double_click_time = 0
stats_button = None
stats_puzzle_menu = None
connect_menu = None
logs_menu = None
all_clients_menu = None
palette_menu = None
palette_var = None
ROW_APPEARANCE_TAG_PREFIX = 'appearance_'
row_appearance_tags = set()


def get_folder_tag(tags):
    """Return the folder path tag stored on a tree row."""
    return next(
        (tag for tag in tags if isinstance(tag, str) and ('\\' in tag or '/' in tag)),
        None
    )


def get_foldit_parent_dir(folder_path):
    clean_path = str(folder_path).strip()
    if not clean_path:
        return ''
    return os.path.dirname(clean_path)


def get_running_foldit_parent_dirs():
    parent_dirs = []
    for item in process_tree.get_children():
        folder_path = get_folder_tag(process_tree.item(item, 'tags'))
        parent_dir = get_foldit_parent_dir(folder_path)
        if parent_dir and parent_dir not in parent_dirs:
            parent_dirs.append(parent_dir)
    return parent_dirs


def get_pid_tag(tags):
    """Return the process id tag stored on a tree row."""
    for tag in tags:
        if isinstance(tag, int):
            return tag
        if isinstance(tag, str) and tag.isdigit():
            return int(tag)
    return None


def get_pid_for_folder(folder_path):
    clean_folder = str(folder_path).strip()
    if not clean_folder:
        return None

    for item in process_tree.get_children():
        tags = process_tree.item(item, 'tags')
        if get_folder_tag(tags) != clean_folder:
            continue
        return get_pid_tag(tags)
    return None


def get_puzzle_id(pid):
    puzzle_number = monitored_processes.get(pid, {}).get('puzzle_number') if pid is not None else None
    return str(puzzle_number).strip() if puzzle_number else None


def clean_artifact_name_part(value, fallback="unknown"):
    text = str(value or "").strip()
    text = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", text)
    text = re.sub(r"\s+", " ", text).strip().strip(". ")
    return text or fallback


def build_artifact_filename(row, kind, suffix, save_name=None):
    timestamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    parts = [
        timestamp,
        row.get("client_name") or row.get("client") or "client",
        row.get("puzzle_id") or "puzzle",
    ]
    if save_name:
        parts.append(save_name)
    else:
        parts.extend([
            row.get("script_type") or "script",
            row.get("score") or "score",
        ])
    stem = "_".join(clean_artifact_name_part(part) for part in parts if str(part or "").strip())
    return sanitize_artifact_filename(f"{stem}.{suffix}", fallback=f"{timestamp}.{suffix}")


def get_cached_artifact_row(row_id):
    clean_row_id = str(row_id or "").strip()
    if not clean_row_id:
        return None
    with artifact_row_cache_lock:
        row = artifact_row_cache.get(clean_row_id)
        return dict(row) if row else None


def find_latest_ir_solution(folder):
    ir_files = [
        os.path.join(folder, name)
        for name in os.listdir(folder)
        if name.endswith(".ir_solution")
    ]
    if not ir_files:
        return None
    return max(ir_files, key=os.path.getmtime)


def build_remote_artifact(kind, row_id, address, connection_id):
    """Build an artifact requested by a connected peer from a cached local row."""
    clean_kind = str(kind or "").strip().lower()
    row = get_cached_artifact_row(row_id)
    if not row:
        raise RuntimeError("Remote row is no longer available")

    folder = row.get("folder")
    if not folder or not os.path.isdir(folder):
        raise RuntimeError("Client folder is no longer available")

    if clean_kind == "log":
        script_path = os.path.join(folder, "scriptlog.default.xml")
        if not os.path.exists(script_path):
            raise FileNotFoundError("scriptlog.default.xml was not found")

        log_data = foldit_log_handler.get_fresh_data(script_path)
        if log_data:
            row["script_type"] = log_data.get("script_type") or row.get("script_type")
            score = log_data.get("highest_score") or log_data.get("script_highest_score")
            row["score"] = str(score).split(".")[0] if score is not None else row.get("score")

        return {
            "kind": "log",
            "path": script_path,
            "filename": build_artifact_filename(row, "log", "txt"),
        }

    if clean_kind == "pdb":
        if export_pdb is None:
            raise RuntimeError("savefile_api is unavailable")

        ir_path = find_latest_ir_solution(folder)
        if not ir_path:
            raise FileNotFoundError("No .ir_solution file was found")

        save_name = None
        if get_basic_info is not None:
            try:
                save_info = get_basic_info(ir_path)
                save_name = " ".join(str(save_info.save_name).split())
            except Exception:
                save_name = None
        if not save_name:
            save_name = os.path.splitext(os.path.basename(ir_path))[0]

        temp_file = tempfile.NamedTemporaryFile(
            prefix="foldit_remote_pdb_",
            suffix=".pdb",
            delete=False,
        )
        temp_path = temp_file.name
        temp_file.close()
        try:
            export_pdb(ir_path, temp_path)
        except Exception:
            try:
                os.remove(temp_path)
            except OSError:
                pass
            raise

        return {
            "kind": "pdb",
            "path": temp_path,
            "filename": build_artifact_filename(row, "pdb", "pdb", save_name=save_name),
            "cleanup_path": temp_path,
        }

    raise RuntimeError(f"Unsupported artifact kind: {kind}")


def build_remote_artifact_query(kind, query, address, connection_id):
    """Build an artifact requested by a connected peer from a log lookup query."""
    clean_kind = str(kind or "").strip().lower()
    clean_query = dict(query or {})
    if clean_kind != "log":
        raise RuntimeError(f"Unsupported artifact query kind: {kind}")

    match_path = find_matching_log_file(
        clean_query,
        get_known_client_log_roots(clean_query.get("client_name")),
    )
    if match_path:
        return {
            "kind": "log",
            "path": match_path,
            "filename": os.path.basename(match_path),
        }

    export_path = export_matching_live_log(clean_query, open_after=False)
    if export_path:
        return {
            "kind": "log",
            "path": export_path,
            "filename": os.path.basename(export_path),
        }

    raise FileNotFoundError("No matching log was found")


def get_post_copy_shortcut(source_puzzle_id, target_puzzle_id):
    source_id = str(source_puzzle_id).strip() if source_puzzle_id else ""
    target_id = str(target_puzzle_id).strip() if target_puzzle_id else ""
    if source_id and target_id and source_id != target_id:
        return "ctrl+p"
    return "ctrl+o"


def clamp(value, minimum, maximum):
    return max(minimum, min(maximum, value))


def format_client_column_value(folder_name):
    return re.sub(r'foldit', 'f', str(folder_name), flags=re.IGNORECASE)


def client_lookup_keys(client_name):
    clean_name = str(client_name or "").strip()
    keys = {clean_name, format_client_column_value(clean_name)}
    keys.add(clean_name.replace("oldit", ""))
    return {key for key in keys if key}


def remember_client_log_root(client_name, folder):
    clean_folder = str(folder or "").strip()
    if not clean_folder:
        return
    for key in client_lookup_keys(client_name):
        client_log_roots_by_client[key].add(clean_folder)


def get_known_client_log_roots(client_name):
    roots = set()
    for key in client_lookup_keys(client_name):
        roots.update(client_log_roots_by_client.get(key, set()))
    return sorted(roots)


def _score_from_log_data(log_data):
    if not log_data:
        return None
    score = log_data.get("highest_score")
    if score is None:
        score = log_data.get("script_highest_score")
    return score


def export_matching_live_log(query, open_after=False):
    clean_query = dict(query or {})
    for folder in get_known_client_log_roots(clean_query.get("client_name")):
        script_path = os.path.join(folder, "scriptlog.default.xml")
        if not os.path.exists(script_path):
            continue
        log_data = foldit_log_handler.get_data(script_path) or foldit_log_handler.get_fresh_data(script_path)
        if not log_data:
            continue
        if not values_match_log_query(
            clean_query,
            client_name=clean_query.get("client_name"),
            puzzle_id=clean_query.get("puzzle_id"),
            script_type=log_data.get("script_type"),
            score=_score_from_log_data(log_data),
        ):
            continue
        return foldit_log_handler.export_log(
            folder,
            open_file=bool(open_after),
            puzzle_id=clean_query.get("puzzle_id"),
        )
    return None


def parse_hex_color(color_value):
    clean = str(color_value).strip()
    if not re.fullmatch(r"#[0-9a-fA-F]{6}", clean):
        return None
    return tuple(int(clean[idx:idx + 2], 16) for idx in (1, 3, 5))


def blend_hex_colors(start_color, end_color, ratio):
    start_rgb = parse_hex_color(start_color)
    end_rgb = parse_hex_color(end_color)
    if start_rgb is None or end_rgb is None:
        return end_color if ratio >= 1.0 else start_color

    ratio = clamp(float(ratio), 0.0, 1.0)
    channels = [
        round(start + (end - start) * ratio)
        for start, end in zip(start_rgb, end_rgb)
    ]
    return "#{:02x}{:02x}{:02x}".format(*channels)


def get_score_fade_ratio(stale_ticks):
    tick_limit = max(1, int(settings_manager.STALE_TICK_LIMIT))
    return clamp(stale_ticks / tick_limit, 0.0, 1.0)


def is_row_appearance_tag(tag):
    return isinstance(tag, str) and tag.startswith(ROW_APPEARANCE_TAG_PREFIX)


def encode_appearance_color(color_value):
    if not color_value:
        return 'none'
    return str(color_value).strip().lower().lstrip('#')


def update_score_stale_ticks(process_state, is_idle, score_value, script_change_token):
    previous_score = parse_numeric_score(process_state.get('last_score_value'))
    previous_ticks = max(0, int(process_state.get('score_stale_ticks', 0) or 0))
    previous_idle = bool(process_state.get('was_idle', False))
    try:
        previous_token = max(0, int(process_state.get('last_script_change_token', 0) or 0))
    except (TypeError, ValueError):
        previous_token = 0
    try:
        current_token = max(0, int(script_change_token or 0))
    except (TypeError, ValueError):
        current_token = 0

    current_score = parse_numeric_score(score_value)
    if (
        is_idle
        or previous_idle
        or current_score is None
        or previous_score is None
        or current_token != previous_token
        or abs(current_score - previous_score) > 1e-9
    ):
        stale_ticks = 0
    else:
        stale_ticks = previous_ticks + 1

    process_state['score_stale_ticks'] = stale_ticks
    process_state['last_score_value'] = current_score
    process_state['last_script_change_token'] = current_token
    process_state['was_idle'] = bool(is_idle)
    return stale_ticks


def resolve_row_appearance(
    base_foreground,
    is_window_visible,
    is_idle,
    has_score_mismatch,
    is_selected_source,
    stale_ticks,
):
    font_key = 'bold' if is_window_visible else 'normal'
    foreground = settings_manager.IDLE_FONT_COLOR if is_idle else base_foreground
    fade_ratio = get_score_fade_ratio(stale_ticks)
    if not is_idle and fade_ratio > 0.0:
        foreground = blend_hex_colors(
            base_foreground,
            settings_manager.STALE_FONT_COLOR,
            fade_ratio,
        )

    background = None
    if is_selected_source:
        background = settings_manager.SELECTED_SOURCE_BACKGROUND_COLOR
    elif is_idle:
        background = settings_manager.IDLE_BACKGROUND_COLOR
    elif has_score_mismatch:
        background = settings_manager.MISMATCH_BACKGROUND_COLOR

    return font_key, foreground, background


def ensure_row_appearance_tag(treeview, font_key, foreground, background):
    tag_name = (
        f"{ROW_APPEARANCE_TAG_PREFIX}"
        f"{font_key}_{encode_appearance_color(foreground)}_{encode_appearance_color(background)}"
    )
    if tag_name not in row_appearance_tags:
        tag_options = {
            'font': bold_font if font_key == 'bold' else normal_font,
        }
        if foreground:
            tag_options['foreground'] = foreground
        if background:
            tag_options['background'] = background
        treeview.tag_configure(tag_name, **tag_options)
        row_appearance_tags.add(tag_name)
    return tag_name


def build_row_appearance_tag(
    treeview,
    base_foreground,
    is_window_visible,
    is_idle,
    has_score_mismatch,
    is_selected_source,
    stale_ticks,
):
    appearance = resolve_row_appearance(
        base_foreground=base_foreground,
        is_window_visible=is_window_visible,
        is_idle=is_idle,
        has_score_mismatch=has_score_mismatch,
        is_selected_source=is_selected_source,
        stale_ticks=stale_ticks,
    )
    return ensure_row_appearance_tag(treeview, *appearance)


def apply_row_appearance(item_id, base_foreground=None):
    if not process_tree.exists(item_id):
        return

    base_foreground = base_foreground or settings_manager.NORMAL_FONT_COLOR
    tags = [
        tag for tag in process_tree.item(item_id, 'tags')
        if not is_row_appearance_tag(tag)
    ]
    pid = get_pid_tag(tags)
    process_state = monitored_processes.get(pid, {}) if pid is not None else {}
    try:
        stale_ticks = max(0, int(process_state.get('score_stale_ticks', 0) or 0))
    except (TypeError, ValueError):
        stale_ticks = 0

    appearance_tag = build_row_appearance_tag(
        process_tree,
        base_foreground=base_foreground,
        is_window_visible='active_window' in tags,
        is_idle='idle_window' in tags,
        has_score_mismatch='score_mismatch' in tags,
        is_selected_source='selected_source' in tags,
        stale_ticks=stale_ticks,
    )
    tags.append(appearance_tag)
    process_tree.item(item_id, tags=tags)

#--------------------------------------------------------------------------------------------------------------PROCESS MONITOR

def get_foldit_clients():
    return window_manager.list_foldit_clients()


def update_process_cpu_usage(clients):
    """Update the CPU usage of processes and check thresholds."""
    global monitored_processes
    current_time = time.time()
    
    for client in clients:
        proc = client.process
        pid = client.pid
        try:
            cpu_usage = proc.cpu_percent(interval=None)
            monitored_processes[pid]['cpu_history'].append((current_time, cpu_usage))
            cpu_history_copy = list(monitored_processes[pid]['cpu_history'])
            if 'high_cpu_state' not in monitored_processes[pid]:
                monitored_processes[pid]['high_cpu_state'] = False
            high_cpu_count = sum(1 for _, usage in cpu_history_copy if usage > settings_manager.HIGH_CPU_THRESHOLD)
            if high_cpu_count / max(1, len(cpu_history_copy)) >= 0.90:
                monitored_processes[pid]['high_cpu_state'] = True
            low_cpu_count = sum(1 for _, usage in cpu_history_copy if usage < settings_manager.LOW_CPU_THRESHOLD)
            if low_cpu_count / max(1, len(cpu_history_copy)) >= 0.90:
                if monitored_processes[pid]['high_cpu_state']:
                    play_alert_sound()
                    monitored_processes[pid]['high_cpu_state'] = False
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            if pid in monitored_processes:
                del monitored_processes[pid]

def schedule_update():
    """Schedule the periodic update of the process list."""
    if is_ui_interacting():
        root.after(200, schedule_update)
        return

    update_process_list()
    if network_manager.has_clients():
        network_manager.send_tree_data()
    root.after(settings_manager.CHECK_INTERVAL * 1000, schedule_update)
    
def update_process_list():
    """Update the process list in the GUI."""
    global monitored_processes, artifact_row_cache

    clients = get_foldit_clients()
    update_process_cpu_usage(clients)
    
    # Capture selected folders up front so we can keep the selection tag during
    # the refresh instead of removing and restoring it after the repaint.
    existing_items = set(process_tree.get_children())
    selected_folder_order = []
    for item in selected_rows:
        if not process_tree.exists(item):
            continue
        folder_tag = get_folder_tag(process_tree.item(item, 'tags'))
        if folder_tag and folder_tag not in selected_folder_order:
            selected_folder_order.append(folder_tag)
    selected_folders = set(selected_folder_order)
    current_selected_items = {}
    current_items = set()
    current_artifact_rows = {}
    speed_boost_states = {
        int(client.pid): {
            "pid": int(client.pid),
            "client_name": client.client_name,
            "is_window_visible": bool(client.is_window_visible),
            "script_running": False,
        }
        for client in clients
    }
    
    default_row_foreground = settings_manager.NORMAL_FONT_COLOR
    ttk.Style().configure('Treeview', foreground=default_row_foreground)
    
    for client in clients:
        try:
            pid = client.pid
            folder = client.folder
            if not folder:
                continue
            remember_log_root = globals().get("remember_client_log_root")
            if remember_log_root is not None:
                remember_log_root(client.client_name, folder)
            process_state = monitored_processes[pid]
            cpu_history = process_state['cpu_history']
            cpu_percent = sum(usage for _, usage in cpu_history) / len(cpu_history) if cpu_history else 0.0
            client_column_value = format_client_column_value(client.client_name)
            is_window_visible = client.is_window_visible

            # Get the puzzle number from the window title
            puzzle_number = get_puzzle_number(client.window_title)
            process_state['puzzle_number'] = puzzle_number

            score_display = ""
            highest_score = None
            script_highest_score = None
            script_type = ""
            script_change_token = 0
            script_running = False
            has_score_mismatch = False
            script_path = os.path.join(folder, "scriptlog.default.xml")
            foldit_log_handler.start_monitoring(script_path)
            log_data = foldit_log_handler.get_data(script_path)
            if log_data:
                script_type = log_data.get('script_type', '')
                script_running = bool(log_data.get('run_open', False))
                highest_score = log_data.get('highest_score')
                script_highest_score = log_data.get('script_highest_score')
                try:
                    script_change_token = max(0, int(log_data.get('script_change_token', 0) or 0))
                except (TypeError, ValueError):
                    script_change_token = 0
                process_state['last_log_lines'] = deque(
                    log_data.get('last_log_lines', []),
                    maxlen=settings_manager.tooltip_lines
                )
                process_state['last_log_update'] = time.time()
                has_score_mismatch = (
                    highest_score is not None
                    and script_highest_score is not None
                    and abs(float(highest_score) - float(script_highest_score)) > 1e-9
                )

            score_display = ""
            if highest_score is not None:
                score_display = f"{str(highest_score).split('.')[0] + '.' + str(highest_score).split('.')[1][:1]}"

            # Create a unique identifier for the process
            item_id = str(pid)
            current_items.add(item_id)
            
            # Update an existing item or create a new one
            tags = [pid, folder]
            is_idle = (not is_window_visible) and cpu_percent < settings_manager.LOW_CPU_THRESHOLD
            if is_window_visible:
                tags.append('active_window')
            elif is_idle:
                tags.append('idle_window')

            speed_boost_states[pid] = {
                "pid": pid,
                "client_name": client.client_name,
                "is_window_visible": is_window_visible,
                "script_running": script_running,
            }

            alarm_armed = bool(process_state.get('alarm_on_change'))
            alarm_prev_score = (
                parse_numeric_score(process_state.get('last_score_value'))
                if alarm_armed else None
            )

            stale_ticks = update_score_stale_ticks(
                process_state,
                is_idle=is_idle,
                score_value=script_highest_score,
                script_change_token=script_change_token,
            )

            # Per-client "Alarm on change": beep once when the monitored score
            # changes, then disarm. Watches the same script score the stale-fade
            # tracks (monotonic within a script run -> no spurious drops to misfire on).
            if alarm_armed:
                alarm_new_score = parse_numeric_score(script_highest_score)
                if (
                    alarm_prev_score is not None
                    and alarm_new_score is not None
                    and abs(alarm_new_score - alarm_prev_score) > 1e-9
                ):
                    play_alert_sound()
                    process_state['alarm_on_change'] = False

            if has_score_mismatch:
                tags.append('score_mismatch')

            is_selected_source = folder in selected_folders
            if folder in selected_folders:
                if 'selected_source' not in tags:
                    tags.append('selected_source')
                current_selected_items[folder] = item_id

            tags.append(
                build_row_appearance_tag(
                    process_tree,
                    base_foreground=default_row_foreground,
                    is_window_visible=is_window_visible,
                    is_idle=is_idle,
                    has_score_mismatch=has_score_mismatch,
                    is_selected_source=is_selected_source,
                    stale_ticks=stale_ticks,
                )
            )
            
            # Create values with puzzle number
            values = [score_display, f"{cpu_percent:.0f}", client_column_value, script_type]
            if settings_manager.settings['display']['show_puzzle_column']:
                values.append(str(puzzle_number) if puzzle_number else "")

            current_artifact_rows[str(pid)] = {
                "row_id": str(pid),
                "pid": pid,
                "folder": folder,
                "client": client_column_value,
                "client_name": client.client_name,
                "script_type": script_type,
                "score": score_display,
                "puzzle_id": str(puzzle_number) if puzzle_number else "",
            }
            
            if item_id in existing_items:
                process_tree.item(item_id, values=values, tags=tags)
            else:
                process_tree.insert('', 'end', iid=item_id, values=values, tags=tags)
            
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
    
    # Remove items that no longer exist
    items_to_remove = existing_items - current_items
    for item_id in items_to_remove:
        process_tree.delete(item_id)
        try:
            monitored_processes.pop(int(item_id), None)
        except (TypeError, ValueError):
            pass

    with artifact_row_cache_lock:
        artifact_row_cache = current_artifact_rows

    if speed_boost is not None:
        speed_boost.on_clients_refreshed(speed_boost_states.values())
    

    # Sort by the original folder path stored in row tags, not by the displayed text.
    items = [
        (
            os.path.basename(get_folder_tag(process_tree.item(item, 'tags')) or ''),
            item,
        )
        for item in process_tree.get_children()
    ]
    items.sort(key=lambda x: natural_sort(x[0]))
    
    # Reorder items
    for idx, (_, item) in enumerate(items):
        process_tree.move(item, '', idx)

    current_parent_dirs = get_running_foldit_parent_dirs()
    if current_parent_dirs:
        settings_manager.save_last_seen_foldit_parent(current_parent_dirs[0])
    
    adjust_column_widths(process_tree)
    adjust_window_size(changeWidth=False)
    check_client_changes(clients)

    selected_rows[:] = [
        current_selected_items[folder]
        for folder in selected_folder_order
        if folder in current_selected_items
    ]

def natural_sort(value):
    """Function to perform a natural sort."""
    return [int(s) if s.isdigit() else s.lower() for s in re.split(r'(\d+)', value)]

#-------------------------------------------------------------------------------------------------------------GUI MAIN WINDOW
def adjust_column_widths(treeview):
    """Adjust the column widths to fit their content."""
    for col in treeview['columns']:
        # Reset column width first
        treeview.column(col, width=0)
        
        current_font = normal_font
        
        # Find maximum width considering all items and their fonts
        max_pixels = current_font.measure(treeview.heading(col)['text'])
        
        for item in treeview.get_children():
            # Get item's font based on its tags
            item_tags = treeview.item(item)['tags']
            if 'active_window' in item_tags:
                current_font = bold_font
            else:
                current_font = normal_font
                
            # Measure text width with the appropriate font
            text = str(treeview.set(item, col))  # Convert to string to handle non-string values
            width_pixels = current_font.measure(text)
            max_pixels = max(max_pixels, width_pixels)
        
        # Add padding and set the exact width
        final_width = max_pixels + 10
        treeview.column(col, width=final_width, stretch=False)

def adjust_window_size(changeWidth=True, force=False):
    """Adjust the window size to fit all trees"""
    # Check the size lock flag
    if not force and hasattr(root, 'size_locked') and root.size_locked:
        return
        
    # Set flag for programmatic resize. This flag is used to distinguish between user resize and auto-resize
    root.programmatic_resize = True
    
    total_height = 0
    
    # Calculate row height based on font metrics
    font_height = max(
        normal_font.metrics()['linespace'],
        bold_font.metrics()['linespace'],
        italic_font.metrics()['linespace']
    )
    row_height = font_height + 0  # Add padding (0px top and bottom)
        
    # Set row height for main tree
    style = ttk.Style()
    style.configure('Treeview', rowheight=row_height)

    # Calculate height for main tree.
    # Treeview defaults to ~10 rows if "height" is not set, which can push the bottom button frame
    # out of view when auto-sized for a small number of clients.
    main_items = len(process_tree.get_children())
    visible_main_items = max(1, main_items)
    process_tree.configure(height=visible_main_items)
    header_height = 25
    #header_height = process_tree.winfo_reqheight() - (row_height * main_items)
    main_tree_height = (row_height * visible_main_items) + header_height
    # print (main_tree_height, total_height, row_height, main_items, header_height )
    total_height += main_tree_height


    border_padding = 4
    button_height = 25
    total_height += border_padding + button_height + 2 

    # Force table height to prevent GUI problems #Treeview #remotetreeview bug
    #process_tree.place(x=0, y=0, relwidth=1, height=main_tree_height + border_padding)  

    
    # Add height for remote trees
    if hasattr(root, 'remote_trees'):
        for remote_tree in root.remote_trees.values():
            items_count = len(remote_tree.tree.get_children())
            # Make the remote tree request exactly its row count, like the main tree does.
            # Otherwise it keeps Treeview's default 10-row request and the leftover window
            # space gets split between the panels via expand=True -> empty rows in the top
            # panel and a scrollbar in the bottom one.
            remote_tree.tree.configure(height=max(1, items_count), style='Treeview')
            remote_header_height = remote_tree.header.winfo_reqheight()
            remote_height = (row_height * items_count) + remote_header_height
            total_height += remote_height + 20
    
    # Add fixed heights



    # Set minimum and maximum heights
    min_height = (row_height + header_height) + border_padding + button_height + 2
    max_height = int(root.winfo_screenheight() * 0.8)
    window_height = min(max(min_height, total_height), max_height)
    window_width = max(240, process_tree.winfo_reqwidth() + 1)
    root.geometry(f"{window_width}x{window_height}")
    # Save the last automatic size
    if not force:
        root.last_auto_size = (window_width, window_height)
    
    # Clear programmatic resize flag after short delay
    root.after(100, lambda: setattr(root, 'programmatic_resize', False))


def make_window_draggable(window):
    """Make the window draggable by clicking anywhere on it."""
    window.is_dragging = False
    window.last_update_time = 0
    
    def start_move(event):
        if time.time() - last_double_click_time > 1.5:
            window.is_dragging = True
            window.x = event.x
            window.y = event.y
            
    def stop_move(event):
        window.is_dragging = False
        
    def do_move(event):
        if time.time() - last_double_click_time > 1.5 and window.is_dragging:
            deltax = event.x - window.x
            deltay = event.y - window.y
            x = window.winfo_x() + deltax
            y = window.winfo_y() + deltay
            window.geometry(f"+{x}+{y}")
            
    window.bind("<Button-1>", start_move)
    window.bind("<ButtonRelease-1>", stop_move)
    window.bind("<B1-Motion>", do_move)


def on_window_state_change(event):
    """Handle window state change events."""
    #print("Window state changed:", root.state())

def on_close():
    """Handle application closing"""
    if speed_boost is not None:
        speed_boost.shutdown()
    foldit_log_handler.stop_all_monitoring()
    stats_manager.flush_all()
    stats_window = get_open_stats_window()
    if stats_window is not None:
        try:
            stats_window._save_window_position()
        except Exception:
            pass
    if 'root' in globals() and root.winfo_exists():
        settings_manager.save_window_position(root.winfo_x(), root.winfo_y())
    if 'network_manager' in globals():
        network_manager.shutdown()
    root.destroy()


#----------------------------------------------------------------------------------------------------------- TOOLTIP WINDOW
def setup_tooltip(root):
    """Настройка подсказок"""
    fonts = {'tooltip': tooltip_font}
    tooltip = TooltipWindow(root, fonts)
    
    def get_tooltip_text(item):
        """Получение текста подсказки для элемента"""
        tags = process_tree.item(item, 'tags')
        folder_path = next((tag for tag in tags if '\\' in tag or '/' in tag), None)
        if folder_path:
            return get_last_log_lines(folder_path)
        return None
    
    tooltip.set_update_callback(get_tooltip_text)
    
    def on_enter(event, tree_widget=None, is_remote=False):
        tree = tree_widget if tree_widget else process_tree
        column = tree.identify_column(event.x)
        item = tree.identify('item', event.x, event.y)
        
        if column == "#1" and item:
            if is_remote:
                tree_id = getattr(tree.master, '_name', None)
                remote_tree = root.remote_trees.get(tree_id) if tree_id else None
                if remote_tree and item in remote_tree.log_data:
                    log_lines = remote_tree.log_data[item]
                    # Each stored line already carries its own trailing newline, so strip
                    # any CR/LF per line and re-join with a single "\n" (matches the local
                    # tooltip and stays correct regardless of the sender's OS line endings).
                    log_text = "\n".join(
                        "{:5d}:  {}".format(num, str(line).rstrip("\r\n"))
                        for num, line in log_lines
                    )
                else:
                    log_text = "Log data not available"
            else:
                tags = tree.item(item, 'tags')
                folder_path = next((tag for tag in tags if '\\' in tag or '/' in tag), None)
                if folder_path:
                    log_text = get_last_log_lines(folder_path)
                else:
                    log_text = "Log data not available"
                
            x = root.winfo_x()
            y = root.winfo_y()
            tooltip.delayed_show(log_text, x, y, item, column, tree)

    def on_leave(event):
        tooltip.hide()

    process_tree.bind('<Motion>', lambda e: on_enter(e))
    process_tree.bind('<Leave>', on_leave)
    
    def bind_remote_tree(remote_tree):
        remote_tree.tree.bind('<Motion>', 
            lambda e: on_enter(e, tree_widget=remote_tree.tree, is_remote=True))
        remote_tree.tree.bind('<Leave>', on_leave)
    
    tooltip.bind_remote_tree = bind_remote_tree
    
    return tooltip

def get_last_log_lines(folder_path):
    """Get the last lines of the log from the FolditLogHandler"""
    script_path = os.path.join(folder_path, "scriptlog.default.xml")
    data = foldit_log_handler.get_data(script_path)  # Get log data using foldit_log_handler
    
    if not data:
        return "Log data not available"

    max_length = settings_manager.MAX_LINE_LENGTH  # Get max length from settings
    last_lines = data['last_log_lines']  # Get the last log lines
    formatted_lines = []
    
    # Добавляем первую строку с названием папки, скриптом и счетом
    folder_name = os.path.basename(folder_path)
    script_name = data.get('script_type', 'Unknown')
    highest_score = data.get('highest_score', 'N/A')
    header = f"{folder_name} | {script_name} | {highest_score}"
    formatted_lines.append(header+"\n")
    
    def trim_long_line(line, max_length):
        """Trim long line with smart tab handling and middle truncation"""
        def visual_length(s, tab_size=4):
            # Handle different types of tabs including non-breaking spaces
            return len(s.replace('\t', ' ' * tab_size).replace('\u2003', ' ' * tab_size).replace('\u00A0', ' ' * tab_size))  
        
        def replace_tabs(s, tab_size):
            # Replace various types of tabs and spaces
            return s.replace('\t', ' ' * tab_size).replace('\u2003', ' ' * tab_size).replace('\u00A0', ' ' * tab_size)  

        original_line = line
        current_tab_size = 4
        
        # Reduce tab size until line fits or minimum tab size reached
        while current_tab_size >= 1 and visual_length(line, current_tab_size) > max_length:
            current_tab_size -= 1
        
        # Apply best tab replacement
        if current_tab_size >= 1:
            line = replace_tabs(original_line, current_tab_size)
        
        # If still too long after tab replacement, trim middle
        if len(line) > max_length:
            max_allowed = max_length - 3  # 3 symbols for "..."
            start_len = max_allowed // 2
            end_len = max_allowed - start_len
            return f"{line[:start_len]}...{line[-end_len:]}"
        return line

    for line_num, line in last_lines:
        trimmed_line = trim_long_line(line, max_length)            
        formatted_lines.append(f"{line_num:5d}:  {trimmed_line}")
    
    formatted_lines[-1] = formatted_lines[-1].rstrip()  # Remove trailing newline
    return "".join(formatted_lines)  # Return formatted log lines

def on_focus_out(event):
    """Handler for losing focus of the window"""
    global selected_source_item
    # if selected_source_item:
    #     clear_selections(selected_source_item, None)
    #     selected_source_item = None

def get_puzzle_number(window_title):
    """Extracts the first likely puzzle id from the Foldit window title."""
    if not window_title:
        return None
    match = PUZZLE_ID_RE.search(window_title)
    if match:
        return match.group("puzzle")
    return None


def open_stats_for_puzzle(puzzle_id):
    clean_puzzle_id = str(puzzle_id).strip()
    if clean_puzzle_id:
        show_stats(
            root,
            stats_manager,
            settings_manager,
            clean_puzzle_id,
            log_lookup_handler=handle_stats_log_lookup,
        )


def refresh_stats_puzzle_menu():
    if stats_button is None or stats_puzzle_menu is None:
        return

    puzzles = stats_manager.get_active_puzzles()
    stats_puzzle_menu.delete(0, tk.END)

    if puzzles:
        for puzzle_id in puzzles:
            stats_puzzle_menu.add_command(
                label=puzzle_id,
                command=lambda pid=puzzle_id: open_stats_for_puzzle(pid),
            )
        stats_button.state(["!disabled"])
        return

    stats_puzzle_menu.add_command(label="No active puzzles", state="disabled")
    stats_button.state(["disabled"])


def toggle_stats_window():
    """LMB on Stats: open the last viewed puzzle, or close the stats window if it is open."""
    if get_open_stats_window() is not None:
        close_stats_window_if_exists()
        return

    puzzles = stats_manager.get_active_puzzles()
    if not puzzles:
        return
    last_puzzle = str(settings_manager.STATS_LAST_PUZZLE).strip()
    open_stats_for_puzzle(last_puzzle if last_puzzle in puzzles else puzzles[0])


def show_stats_puzzle_menu(event=None):
    """RMB on Stats: menu with all active puzzles."""
    refresh_stats_puzzle_menu()
    if stats_button is None or stats_puzzle_menu is None:
        return "break"

    if not stats_manager.get_active_puzzles() or "disabled" in stats_button.state():
        return "break"

    x = stats_button.winfo_rootx()
    y = stats_button.winfo_rooty() + stats_button.winfo_height()
    try:
        stats_puzzle_menu.tk_popup(x, y)
    finally:
        stats_puzzle_menu.grab_release()
    return "break"

def check_client_changes(clients=None):
    """Проверяет изменения в состоянии клиентов"""
    current_stats_clients = set()
    current_client_runtime = {}
    active_script_paths = set()
    current_clients = clients if clients is not None else get_foldit_clients()
    
    for client in current_clients:
        try:
            folder = client.folder
            if not folder:
                continue
            remember_log_root = globals().get("remember_client_log_root")
            if remember_log_root is not None:
                remember_log_root(client.client_name, folder)
            client_name = client.client_name
            
            script_path = os.path.join(folder, "scriptlog.default.xml")
            active_script_paths.add(script_path)
            foldit_log_handler.start_monitoring(script_path)
            handler = foldit_log_handler.current_handlers.get(script_path)
            puzzle_number = monitored_processes.get(client.pid, {}).get('puzzle_number')
            puzzle_id = str(puzzle_number) if puzzle_number else None

            if puzzle_id:
                current_stats_clients.add(client_name)
                stats_manager.touch_client(client_name, puzzle_id)
                item_id = str(client.pid)
                cpu_percent = 0.0
                is_idle = False
                score_stale_ticks = 0
                if process_tree.exists(item_id):
                    values = process_tree.item(item_id, 'values')
                    tags = process_tree.item(item_id, 'tags')
                    if len(values) > 1:
                        try:
                            cpu_percent = float(values[1])
                        except (TypeError, ValueError):
                            cpu_percent = 0.0
                    is_idle = 'idle_window' in tags
                process_state = monitored_processes.get(client.pid, {})
                try:
                    score_stale_ticks = max(0, int(process_state.get('score_stale_ticks', 0) or 0))
                except (TypeError, ValueError):
                    score_stale_ticks = 0
                current_client_runtime[client_name] = {
                    'puzzle_id': puzzle_id,
                    'cpu_percent': cpu_percent,
                    'is_idle': is_idle,
                    'score_stale_ticks': score_stale_ticks,
                }
            
            if handler and puzzle_id:
                for event in handler.consume_stats_events():
                    event_kind = str(event.get('kind', '')).strip().lower()
                    if event_kind == 'script':
                        stats_manager.handle_monitor_update(
                            client_name=client_name,
                            puzzle_id=puzzle_id,
                            script_name=event.get('script'),
                            score=event.get('score'),
                            continue_tail=bool(event.get('continue_tail', True)),
                        )
                    elif event_kind == 'state':
                        stats_manager.handle_script_state_snapshot(
                            client_name=client_name,
                            puzzle_id=puzzle_id,
                            script_name=event.get('script'),
                            score=event.get('score'),
                        )
                    elif event_kind == 'finish':
                        speed_boost_integration = globals().get("speed_boost")
                        if speed_boost_integration is not None:
                            speed_boost_integration.on_script_finished(client.pid)
                        foldit_log_handler.export_log(
                            folder,
                            open_file=False,
                            puzzle_id=puzzle_id,
                        )
                    
        except Exception as e:
            print(f"Error checking client changes: {e}")

    for monitored_path in list(foldit_log_handler.current_handlers.keys()):
        if monitored_path not in active_script_paths:
            foldit_log_handler.stop_monitoring(monitored_path)

    stats_manager.sync_active_clients(current_stats_clients)
    stats_manager.sync_client_runtime(current_client_runtime)
    refresh_stats_puzzle_menu()
    stats_manager.maybe_autosave()

#--------------------------------------------------------------------------------------------------------------------------NETWORKING
reconnect_targets = set()   # {(address, port)} kept alive while auto-reconnect is on
reconnect_pending = set()   # {(address, port)} currently being dialed
RECONNECT_INTERVAL = 15     # seconds between gentle reconnect sweeps (don't hammer the server)

def connect_to_remote_async(address, port, show_error=True):
    def worker():
        success = network_manager.connect_to_server(address, port)
        if success:
            return

        if show_error:
            root.after(
                0,
                lambda: messagebox.showerror("Error", f"Connection to {address}:{port} failed")
            )

    threading.Thread(target=worker, daemon=True).start()

def show_connect_dialog():
    dialog = ConnectDialog(
        root,
        settings_manager.DEFAULT_ADDRESS,
        settings_manager.DEFAULT_PORT,
        default_auto_reconnect=settings_manager.NETWORK_AUTO_RECONNECT,
    )
    root.wait_window(dialog.dialog)
    if dialog.result:
        address, port, auto_reconnect = dialog.result
        settings_manager.save_network_auto_reconnect(auto_reconnect)
        if auto_reconnect:
            reconnect_targets.add((address, port))
        else:
            reconnect_targets.discard((address, port))
        connect_to_remote_async(address, port)

def create_remote_tree(address, connection_id, port=None):
    tree_id = f'remote_tree_{address}_{connection_id}'

    if not network_manager.has_connection(address, connection_id):
        return

    if not hasattr(root, 'remote_trees'):
        root.remote_trees = {}
    
    if tree_id not in root.remote_trees:
        root.size_locked = False #remove window size lock to add new tree

        # If we're reviving a connection, drop any stale panel for the same host whose
        # underlying connection is already gone (incl. a just-dropped one not yet marked
        # dead), so we never end up with a leftover panel next to the fresh one. Panels
        # of still-live connections (e.g. a second connection to the same host) are kept.
        stale_prefix = f'remote_tree_{address}_'
        for other_id, other_tree in list(root.remote_trees.items()):
            if other_id == tree_id or getattr(other_tree, 'address', None) != address:
                continue
            other_cid = other_id[len(stale_prefix):] if other_id.startswith(stale_prefix) else None
            if other_cid is not None and not network_manager.has_connection(address, other_cid):
                other_tree.frame.destroy()
                del root.remote_trees[other_id]

        # Define the displayed connection ID
        display_id = ""
        if port and port != settings_manager.DEFAULT_PORT:
            display_id = f":{port}"
        
        fonts = {
            'normal': normal_font,
            'bold': bold_font,
            'italic': italic_font
        }
        inactive_row_colors = {
            'normal_foreground': settings_manager.NORMAL_FONT_COLOR,
            'foreground': settings_manager.IDLE_FONT_COLOR,
            'background': settings_manager.IDLE_BACKGROUND_COLOR
        }
        
        remote_tree = RemoteTreeView(
            root, 
            address, 
            display_id, 
            fonts,
            show_puzzle_column=settings_manager.settings['display']['show_puzzle_column'],
            inactive_row_colors=inactive_row_colors
        )
        root.remote_trees[tree_id] = remote_tree
        remote_tree.port = port
        remote_tree.network_connection_id = connection_id

        # Set name for remote_tree frame for identification
        remote_tree.frame._name = tree_id
        
        # Bind event handlers
        if hasattr(root, 'tooltip'):
            root.tooltip.bind_remote_tree(remote_tree)
        remote_tree.tree.bind('<Double-1>', lambda e, rt=remote_tree: on_remote_tree_double_click(e, rt))
        remote_tree.tree.bind('<Button-3>', lambda e, rt=remote_tree: show_remote_tree_context_menu(e, rt))
            
        adjust_window_size()
        adjust_column_widths(process_tree)  # Add column width correction
        
        # Add menu item for disconnecting
        base_menu_label = f"Disconnect {address}{display_id}"
        menu_label = unique_menu_item_label(connect_menu, base_menu_label, connection_id)
        
        def remove_menu_item():
            try:
                remove_menu_item_by_label(connect_menu, menu_label)
            except Exception as e:
                print(f"Error removing menu item: {e}")
        
        def disconnect_handler():
            try:
                # Manual disconnect: stop auto-reconnect for this host and remove the panel.
                reconnect_targets.discard((address, port))
                network_manager.disconnect_client(address, connection_id, user_initiated=True)
            except Exception as e:
                print(f"Error in disconnect handler: {e}")
        
        insert_menu_item_before_label(
            connect_menu,
            "Disconnect all",
            menu_label,
            disconnect_handler,
        )
        
        # Register callback for removing menu item when disconnecting
        network_manager.register_disconnect_callback(address, connection_id, remove_menu_item)

def remove_remote_tree(address, connection_id, user_initiated=False):
    tree_id = f'remote_tree_{address}_{connection_id}'
    if not (hasattr(root, 'remote_trees') and tree_id in root.remote_trees):
        return

    remote_tree = root.remote_trees[tree_id]
    port = getattr(remote_tree, 'port', None)

    # Unexpected drop of a kept-alive host: keep the panel but show it's disconnected
    # (frozen data, red header). The reconnect sweep will revive/replace it.
    if (not user_initiated) and ((address, port) in reconnect_targets):
        remote_tree.mark_dead()
        return

    root.size_locked = False #remove window size lock and auto-resize on next update
    remote_tree.frame.destroy()
    del root.remote_trees[tree_id]
    adjust_window_size()

def update_remote_trees():
    """Update all remote trees with their corresponding data"""
    for connection in network_manager.get_connections_snapshot():
        # Only the side that initiated a connection shows a panel for it; connections
        # we merely accepted (incl. our own loopback) must not spawn extra panels.
        if not connection.get('initiated'):
            continue
        address = connection['address']
        connection_id = connection['connection_id']
        tree_id = f'remote_tree_{address}_{connection_id}'

        if not hasattr(root, 'remote_trees'):
            root.remote_trees = {}

        if tree_id not in root.remote_trees:
            create_remote_tree(address, connection_id, connection.get('port'))

        if tree_id not in root.remote_trees:
            continue

        if connection['data'] is not None:
            remote_tree = root.remote_trees[tree_id]
            remote_tree.update_items(connection['data'], capabilities=connection.get('capabilities'))
            adjust_column_widths(remote_tree.tree)


def request_remote_artifact(remote_tree, item, kind, open_after=False, notify=True):
    if getattr(remote_tree, "dead", False):
        if notify:
            messagebox.showerror("Error", "Remote host is disconnected")
        return None
    if not remote_tree.supports_capability(ARTIFACT_TRANSFER_CAPABILITY):
        if notify:
            messagebox.showerror("Error", "Remote host does not support artifact transfer")
        return None

    item_payload = remote_tree.item_payloads.get(item, {})
    row_id = str(item_payload.get("row_id") or "").strip()
    if not row_id:
        if notify:
            messagebox.showerror("Error", "Remote row id is not available")
        return None

    connection_id = getattr(remote_tree, "network_connection_id", None)
    if not connection_id:
        if notify:
            messagebox.showerror("Error", "Remote connection id is not available")
        return None

    try:
        request_id = network_manager.send_artifact_request(
            remote_tree.address,
            connection_id,
            kind,
            row_id,
            open_after=open_after,
            notify=notify,
        )
    except Exception as e:
        if notify:
            messagebox.showerror("Error", f"Failed to request remote artifact: {e}")
        return None

    if request_id is None and notify:
        messagebox.showerror("Error", "Failed to send remote artifact request")
    return request_id


def request_remote_matching_log(query, open_after=True, notify=True):
    count = 0
    if not hasattr(root, "remote_trees"):
        return count

    for remote_tree in root.remote_trees.values():
        if getattr(remote_tree, "dead", False):
            continue
        if not remote_tree.supports_capability(ARTIFACT_QUERY_CAPABILITY):
            continue
        connection_id = getattr(remote_tree, "network_connection_id", None)
        if not connection_id:
            continue
        try:
            request_id = network_manager.send_artifact_query_request(
                remote_tree.address,
                connection_id,
                "log",
                query,
                open_after=open_after,
                notify=notify,
            )
        except Exception as e:
            if notify:
                messagebox.showerror("Error", f"Failed to request remote matching log: {e}")
            continue
        if request_id is not None:
            count += 1

    return count


def handle_stats_log_lookup(query):
    clean_query = dict(query or {})
    match_path = find_matching_log_file(
        clean_query,
        get_known_client_log_roots(clean_query.get("client_name")),
    )
    if match_path:
        open_file(match_path, reveal_end=True)
        return {"status": "opened", "path": match_path, "source": "local"}

    export_path = export_matching_live_log(clean_query, open_after=True)
    if export_path:
        return {"status": "opened", "path": export_path, "source": "live"}

    remote_count = request_remote_matching_log(clean_query, open_after=True, notify=False)
    if remote_count:
        return {"status": "remote_requested", "count": remote_count}
    return {"status": "not_found"}


def handle_remote_artifact_received(metadata):
    path = metadata.get("path")
    kind = metadata.get("kind")
    if not path:
        return

    if kind == "log" and metadata.get("open_after"):
        open_file(path, reveal_end=True)
        return

    if not metadata.get("notify", True):
        return

    if kind == "pdb":
        messagebox.showinfo("Success", f"Remote PDB saved to:\n{path}")
    elif kind == "log":
        messagebox.showinfo("Success", f"Remote log saved to:\n{path}")
    else:
        messagebox.showinfo("Success", f"Remote artifact saved to:\n{path}")


def handle_remote_artifact_error(metadata):
    message = metadata.get("message") or "Remote artifact transfer failed"
    if metadata.get("notify", True):
        messagebox.showerror("Error", message)
    else:
        print(f"Remote artifact error: {message}")


def iter_remote_artifact_rows():
    if not hasattr(root, "remote_trees"):
        return
    for remote_tree in root.remote_trees.values():
        if getattr(remote_tree, "dead", False):
            continue
        if not remote_tree.supports_capability(ARTIFACT_TRANSFER_CAPABILITY):
            continue
        for item in remote_tree.tree.get_children():
            yield remote_tree, item


def open_all_remote_logs():
    count = 0
    for remote_tree, item in iter_remote_artifact_rows() or []:
        if request_remote_artifact(remote_tree, item, "log", open_after=True, notify=False):
            count += 1
    if count == 0:
        messagebox.showinfo("Info", "No remote logs available")


def dump_all_remote_logs():
    count = 0
    for remote_tree, item in iter_remote_artifact_rows() or []:
        if request_remote_artifact(remote_tree, item, "log", open_after=False, notify=False):
            count += 1
    if count:
        messagebox.showinfo("Info", f"Requested {count} remote logs")
    else:
        messagebox.showinfo("Info", "No remote logs available")


def on_remote_tree_double_click(event, remote_tree):
    column = remote_tree.tree.identify_column(event.x)
    item = remote_tree.tree.identify('item', event.x, event.y)
    if not item:
        return "break"
    if column == "#1" or column == "#4":
        request_remote_artifact(remote_tree, item, "log", open_after=True, notify=True)
    return "break"


def show_remote_tree_context_menu(event, remote_tree):
    item = remote_tree.tree.identify('item', event.x, event.y)
    if not item:
        return "break"
    if not remote_tree.supports_capability(ARTIFACT_TRANSFER_CAPABILITY):
        return "break"

    menu = tk.Menu(root, tearoff=0)
    menu.add_command(
        label="Open remote log",
        command=lambda rt=remote_tree, row=item: request_remote_artifact(
            rt, row, "log", open_after=True, notify=True
        ),
    )
    menu.add_command(
        label="Dump remote log",
        command=lambda rt=remote_tree, row=item: request_remote_artifact(
            rt, row, "log", open_after=False, notify=True
        ),
    )
    menu.add_separator()
    menu.add_command(
        label="Export remote PDB",
        command=lambda rt=remote_tree, row=item: request_remote_artifact(
            rt, row, "pdb", open_after=False, notify=True
        ),
    )
    try:
        menu.tk_popup(event.x_root, event.y_root)
    finally:
        menu.grab_release()
    return "break"


def disconnect_all_clients():
    """Disconnect all existing connections and stop auto-reconnect."""
    reconnect_targets.clear()
    reconnect_pending.clear()
    for connection in network_manager.get_connections_snapshot():
        network_manager.disconnect_client(
            connection['address'], connection['connection_id'], user_initiated=True)

    # Remove any frozen "disconnected" panels left over from earlier drops.
    if hasattr(root, 'remote_trees'):
        for dead_id, dead_tree in list(root.remote_trees.items()):
            if getattr(dead_tree, 'dead', False):
                dead_tree.frame.destroy()
                del root.remote_trees[dead_id]
        adjust_window_size()

def connect_to_startup_list():
    """Try to connect to all addresses from startup_connect_list"""
    startup_connect_list = [
        (addr, port) for addr, port in settings_manager.settings['network']['startup_connections']
    ]
    for address, port in startup_connect_list:
        # Startup connections are explicitly configured, so always keep them alive.
        reconnect_targets.add((address, port))
        connect_to_remote_async(address, port, show_error=False)

def is_target_connected(address, port):
    """True if we already have an initiated connection to this address/port."""
    for connection in network_manager.get_connections_snapshot():
        if (connection.get('initiated')
                and connection['address'] == address
                and connection.get('port') == port):
            return True
    return False

def _dial_reconnect(address, port):
    """Dial one reconnect target in the background, clearing its pending flag when done."""
    def worker():
        try:
            network_manager.connect_to_server(address, port)
        except Exception as e:
            print(f"Reconnect to {address}:{port} failed: {e}")
        finally:
            root.after(0, lambda: reconnect_pending.discard((address, port)))
    threading.Thread(target=worker, daemon=True).start()

def schedule_reconnect():
    """Gently re-dial any kept-alive target that is currently disconnected."""
    try:
        for address, port in list(reconnect_targets):
            target = (address, port)
            if target in reconnect_pending or is_target_connected(address, port):
                continue
            reconnect_pending.add(target)
            _dial_reconnect(address, port)
    finally:
        root.after(RECONNECT_INTERVAL * 1000, schedule_reconnect)

#----------------------------------------------------------------------------------------------------------- COPY FOLDIT SAVES
def clear_all_selections():
    """Clearing all selections"""
    global selected_rows
    for item in selected_rows:
        if not process_tree.exists(item):
            continue
        tags = list(process_tree.item(item, 'tags'))
        if 'selected_source' in tags:
            tags.remove('selected_source')
            process_tree.item(item, tags=tags)
            apply_row_appearance(item)
    selected_rows.clear()

def handle_middle_click(event):
    """Middle mouse button click handler: pick source/target rows for copying saves."""
    global selected_rows
    item = process_tree.identify('item', event.x, event.y)
    if not item:
        return "break"

    # If the element is already selected - clear all selections
    if item in selected_rows:
        clear_all_selections()
        return "break"

    # Add selection
    if len(selected_rows) < 2:
        selected_rows.append(item)
        tags = list(process_tree.item(item, 'tags'))
        if 'selected_source' not in tags:
            tags.append('selected_source')
        process_tree.item(item, tags=tags)
        apply_row_appearance(item)

    if len(selected_rows) >= 2:
        # Get tags for source and target
        source_tags = process_tree.item(selected_rows[0], 'tags')
        target_tags = process_tree.item(selected_rows[1], 'tags')
        
        # Get folder paths from tags
        source_folder = get_folder_tag(source_tags)
        target_folder = get_folder_tag(target_tags)
        
        if source_folder and target_folder:
            # Copy files (only saves, no IR solutions)
            copy_foldit_saves(source_folder, target_folder, copy_saves=True, copy_ir=True)

            # Clear selections after 1 second
            root.after(1000, clear_all_selections)

    return "break"

_copy_in_progress = False  # guard: don't start a second copy over a running one


def copy_foldit_saves(source_folder, target_folder, copy_saves=True, copy_ir=False):
    """Copy Foldit saves between two clients.

    The disk work runs in a background thread so a slow or busy disk never freezes
    (or crashes) the GUI; results are marshalled back to the main thread, mirroring
    connect_to_remote_async / _read_share_info."""
    global _copy_in_progress
    if _copy_in_progress:
        return
    job = _resolve_copy_job(source_folder, target_folder, copy_saves, copy_ir)
    _copy_in_progress = True
    threading.Thread(target=_run_copy_jobs, args=([job],), daemon=True).start()


def copy_to_all_clients(source_folder):
    """Copy the latest .ir_solution from source folder to all other clients (threaded)."""
    global _copy_in_progress
    if _copy_in_progress:
        return
    jobs = []
    for item in process_tree.get_children():
        tags = process_tree.item(item, 'tags')
        target_folder = next((tag for tag in tags if '\\' in tag or '/' in tag), None)
        if target_folder and target_folder != source_folder:
            jobs.append(_resolve_copy_job(source_folder, target_folder, copy_saves=False, copy_ir=True))
    if not jobs:
        return
    _copy_in_progress = True
    threading.Thread(target=_run_copy_jobs, args=(jobs,), daemon=True).start()


def _resolve_copy_job(source_folder, target_folder, copy_saves, copy_ir):
    """Gather everything that needs the Tk tree / process state. Runs on the main
    thread before the worker starts (Tkinter must never be touched off-thread)."""
    source_pid = get_pid_for_folder(source_folder)
    target_pid = get_pid_for_folder(target_folder)
    return {
        'source_folder': source_folder,
        'target_folder': target_folder,
        'copy_saves': copy_saves,
        'copy_ir': copy_ir,
        'source_name': os.path.basename(source_folder),
        'target_name': os.path.basename(target_folder),
        'source_pid': source_pid,
        'target_pid': target_pid,
        'source_puzzle_id': get_puzzle_id(source_pid),
        'target_puzzle_id': get_puzzle_id(target_pid),
    }


def _run_copy_jobs(jobs):
    """Worker thread: run the copy jobs sequentially (sequential keeps the disk sane),
    then clear the selection and report any errors back on the main thread."""
    errors = []
    for job in jobs:
        try:
            err = _perform_copy_job(job)
        except Exception as e:  # last-resort guard so the worker never dies silently
            err = f"Unexpected error copying to {job.get('target_name', '?')}: {e}"
        if err:
            errors.append(err)

    def finish():
        global _copy_in_progress
        _copy_in_progress = False
        clear_all_selections()
        if errors:
            messagebox.showerror("Error", "\n".join(errors[:5]))

    root.after(0, finish)


def _perform_copy_job(job):
    """Disk I/O for one copy. Runs in the worker thread: only os/shutil and the
    thread-safe foldit_log_handler here, never Tk. Returns an error string or None;
    schedules stats / the post-copy client shortcut on the main thread via root.after."""
    source_folder = job['source_folder']
    target_folder = job['target_folder']

    # Read the source log first (the handler is internally locked -> safe off-thread).
    source_score = None
    source_log_data = None
    source_snapshot = None
    try:
        source_script_path = os.path.join(source_folder, "scriptlog.default.xml")
        foldit_log_handler.start_monitoring(source_script_path)
        source_log_data = foldit_log_handler.get_fresh_data(source_script_path)
        source_snapshot = foldit_log_handler.get_stats_snapshot(source_script_path, fresh=True)
        if source_log_data:
            source_score = source_log_data.get('script_highest_score')
    except Exception as e:
        print(f"Error reading source log for copy: {e}")

    if job['copy_saves']:
        try:
            src_puzzles = os.path.join(source_folder, "puzzles")
            dest_puzzles = os.path.join(target_folder, "puzzles")
            if not os.path.exists(src_puzzles):
                return f"Source folder does not exist: {src_puzzles}"

            latest_subdir = get_most_recently_modified_subdir(src_puzzles)
            if not latest_subdir:
                return f"No subdirectories found in the folder {src_puzzles}"

            latest_subdir_path = os.path.join(src_puzzles, latest_subdir)
            dest_path = os.path.join(dest_puzzles, latest_subdir)

            if save2backup:
                backup_folder = os.path.join(os.path.dirname(source_folder), backup_folder_name)
                if not os.path.exists(backup_folder):
                    os.makedirs(backup_folder)
                # If the destination already exists, move it aside as a backup.
                if os.path.exists(dest_path):
                    try:
                        current_time = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
                        backup_path = os.path.join(backup_folder,
                            f"{latest_subdir} {current_time} {os.path.basename(target_folder)}")
                        shutil.move(dest_path, backup_path)
                    except Exception as e:
                        print(f"Error creating backup: {e}")
                        shutil.rmtree(dest_path, ignore_errors=True)
            else:
                if os.path.exists(dest_path):
                    shutil.rmtree(dest_path, ignore_errors=True)

            shutil.copytree(latest_subdir_path, dest_path)
        except Exception as e:
            return f"Error copying saves: {e}"

        # Record the copy marker in the source puzzle stats (main thread).
        if job['source_puzzle_id']:
            root.after(0, lambda: stats_manager.handle_copy_saves_event(
                source_client=job['source_name'],
                target_client=job['target_name'],
                puzzle_id=job['source_puzzle_id'],
                source_score=source_score,
                source_script_type=source_log_data.get('script_type') if source_log_data is not None else None,
                source_state_script=source_snapshot.get('script') if source_snapshot is not None else None,
                source_state_score=source_snapshot.get('score') if source_snapshot is not None else None,
            ))

    if job['copy_ir']:
        try:
            ir_files = [f for f in os.listdir(source_folder) if f.endswith('.ir_solution')]
            if ir_files:
                latest_ir = max(ir_files, key=lambda f: os.path.getmtime(os.path.join(source_folder, f)))
                ir_path = os.path.join(source_folder, latest_ir)
                # Only copy a fresh (< 1 day old) solution.
                if time.time() - os.path.getmtime(ir_path) < 3600 * 24:
                    shutil.copy2(ir_path, target_folder)
        except Exception as e:
            return f"Error copying IR solution: {e}"

    # Activate destination window and send the post-copy shortcut (main thread).
    if job['copy_saves'] and job['target_pid'] is not None:
        target_pid = job['target_pid']
        shortcut = get_post_copy_shortcut(job['source_puzzle_id'], job['target_puzzle_id'])
        root.after(0, lambda: window_manager.send_client_shortcut(target_pid, shortcut))

    return None

def export_save_to_pdb(save_path, save_name, puzzle_id=None):
    """Export selected save to puzzle_logs as PDB."""
    try:
        if export_pdb is None:
            raise RuntimeError("savefile_api is unavailable")
        pdb_name = re.sub(r'[<>:"/\\|?*]+', '_', str(save_name)).strip('. ')
        if not pdb_name:
            pdb_name = os.path.splitext(os.path.basename(save_path))[0]
        clean_puzzle_id = str(puzzle_id).strip()
        if clean_puzzle_id:
            pdb_name = f"{clean_puzzle_id} {pdb_name}"
        pdb_path = os.path.join(stats_manager.logs_folder, f"{pdb_name}.pdb")
        export_pdb(save_path, pdb_path)
        messagebox.showinfo("Success", f"PDB exported to:\n{pdb_path}")
    except Exception as e:
        messagebox.showerror("Error", f"Error exporting PDB: {e}")

def get_most_recently_modified_subdir(root_dir):
    """Finds the subdirectory with the most recently modified file"""
    most_recent_time = 0
    most_recent_subdir = None

    for subdir in os.listdir(root_dir):
        subdir_path = os.path.join(root_dir, subdir)
        if os.path.isdir(subdir_path):
            for dirpath, dirnames, filenames in os.walk(subdir_path):
                for filename in filenames:
                    file_path = os.path.join(dirpath, filename)
                    modified_time = os.path.getmtime(file_path)
                    if modified_time > most_recent_time:
                        most_recent_time = modified_time
                        most_recent_subdir = subdir

    return most_recent_subdir

#-------------------------------------------------------------------------------------------------CONTEXT MENU AND SERVICE FUNCTIONS
def show_popup_menu(menu, event):
    if menu is None:
        return "break"
    try:
        menu.tk_popup(event.x_root, event.y_root)
    finally:
        menu.grab_release()
    return "break"


def show_connect_menu(event):
    return show_popup_menu(connect_menu, event)


def show_logs_menu(event):
    return show_popup_menu(logs_menu, event)


def find_menu_item_index(menu, label):
    if menu is None:
        return None
    try:
        end_index = menu.index("end")
    except tk.TclError:
        return None
    if end_index is None:
        return None
    for i in range(end_index + 1):
        try:
            if menu.entrycget(i, "label") == label:
                return i
        except tk.TclError:
            continue
    return None


def remove_menu_item_by_label(menu, label):
    index = find_menu_item_index(menu, label)
    if index is not None:
        menu.delete(index)


def insert_menu_item_before_label(menu, before_label, label, command):
    if menu is None:
        return
    insert_index = find_menu_item_index(menu, before_label)
    if insert_index is None:
        menu.add_command(label=label, command=command)
    else:
        menu.insert(insert_index, "command", label=label, command=command)


def unique_menu_item_label(menu, label, suffix):
    if find_menu_item_index(menu, label) is None:
        return label
    clean_suffix = str(suffix).strip()
    if clean_suffix:
        return f"{label} {clean_suffix}"
    return label


def remove_leading_context_separator():
    while True:
        try:
            if context_menu.index("end") is None or context_menu.type(0) != "separator":
                return
            context_menu.delete(0)
        except tk.TclError:
            return


def dump_all_logs():
    """Saves logs of all active Foldit clients"""
    for item in process_tree.get_children():
        try:
            # Получаем все теги элемента
            tags = process_tree.item(item, 'tags')
            # Ищем тег с путем к папке (содержит \ или /)
            folder_path = next((tag for tag in tags if '\\' in tag or '/' in tag), None)
            if folder_path:
                foldit_log_handler.export_log(
                    folder_path,
                    open_file=False,
                    puzzle_id=get_puzzle_id(get_pid_tag(tags)),
                )
        except Exception as e:
            print(f"Error dumping log for item {item}: {e}")
    messagebox.showinfo("Success", "All logs have been saved")

def open_all_logs():
    """Opens logs of all active Foldit clients"""
    for item in process_tree.get_children():
        try:
            # Получаем все теги элемента
            tags = process_tree.item(item, 'tags')
            # Ищем тег с путем к папке (содержит \ или /)
            folder_path = next((tag for tag in tags if '\\' in tag or '/' in tag), None)
            if folder_path:
                foldit_log_handler.export_log(
                    folder_path,
                    open_file=True,
                    puzzle_id=get_puzzle_id(get_pid_tag(tags)),
                )
                time.sleep(0.3)
        except Exception as e:
            print(f"Error opening log for item {item}: {e}")

def apply_display_palette(palette_name):
    settings_manager.save_active_display_palette(palette_name)
    if palette_var is not None:
        palette_var.set(settings_manager.ACTIVE_DISPLAY_PALETTE)

    ttk.Style().configure('Treeview', foreground=settings_manager.NORMAL_FONT_COLOR)
    for item_id in process_tree.get_children():
        apply_row_appearance(item_id, base_foreground=settings_manager.NORMAL_FONT_COLOR)
    adjust_column_widths(process_tree)

    if 'network_manager' in globals() and network_manager.has_clients():
        network_manager.send_tree_data()

def toggle_always_on_top():
    #global settings_manager.ALWAYS_ON_TOP
    settings_manager.ALWAYS_ON_TOP = not settings_manager.ALWAYS_ON_TOP
    root.attributes("-topmost", settings_manager.ALWAYS_ON_TOP)
    
    # Найти индекс элемента "Always on Top" динамически
    for i in range(context_menu.index("end") + 1):
        try:
            # Проверяем, существует ли элемент
            if context_menu.index(i) >= 0:  # Проверка на существование индекса
                label = context_menu.entrycget(i, "label")
                if "Always on Top" in label:
                    context_menu.entryconfig(i, label="✓ Always on Top" if settings_manager.ALWAYS_ON_TOP else "Always on Top")
                    break
        except tk.TclError:
            # Игнорируем ошибку, если элемент не существует
            continue

share_info_cache = {}  # folder -> info dict filled in by _read_share_info worker


def _read_share_info(folder, info, previous):
    """Worker: find the latest .ir_solution in folder and parse its name/score."""
    try:
        ir_files = [f for f in os.listdir(folder) if f.endswith('.ir_solution')]
        if ir_files:
            latest_ir = max(ir_files, key=lambda f: os.path.getmtime(os.path.join(folder, f)))
            info['path'] = os.path.join(folder, latest_ir)
            info['mtime'] = os.path.getmtime(info['path'])
            if (previous and previous.get('path') == info['path']
                    and previous.get('mtime') == info['mtime'] and previous.get('name')):
                info['name'] = previous['name']
                info['score'] = previous['score']
            elif get_basic_info is not None:
                data = get_basic_info(info['path'])
                info['name'] = " ".join(str(data.save_name).split())
                info['score'] = f"{data.foldit_score:.1f}"
    except Exception as e:
        print(f"Error reading save info for {folder}: {e}")
    finally:
        info['done'] = True


def get_share_info(folder, wait=0.35):
    """Latest-save info for a client folder, waiting at most `wait` seconds.

    The disk read runs in a background thread so a slow or locked save never
    freezes the UI; if it is not done in time the caller gets a partial dict
    and the finished result is picked up on the next call."""
    info = share_info_cache.get(folder)
    if info is None or info.get('done'):
        previous = info
        info = {'done': False}
        share_info_cache[folder] = info
        info['thread'] = threading.Thread(target=_read_share_info, args=(folder, info, previous), daemon=True)
        info['thread'].start()
    thread = info.get('thread')
    if thread is not None and thread.is_alive():
        thread.join(wait)
    return info


STATS_TO_MAIN_LABEL = "To Main"
STATS_TO_FINALIZATION_LABEL = "To Finalization"
def get_client_stats_identity(row_id):
    """(client_name, puzzle_id) for a process-tree row, or None when the row has no
    active puzzle. Stats key clients by client_name on a recognized puzzle id, which
    is exactly what the per-pid artifact cache stores."""
    row = get_cached_artifact_row(row_id)
    if not row:
        return None
    client_name = str(row.get("client_name", "")).strip()
    puzzle_id = str(row.get("puzzle_id", "")).strip()
    if not client_name or not puzzle_id:
        return None
    return client_name, puzzle_id


def move_client_stats_target(puzzle_id, client_name, target):
    """Flip a client between the Main and Finalization stats tables from the main
    window's client context menu."""
    try:
        stats_manager.set_client_target(puzzle_id, client_name, target)
    except Exception as e:
        print(f"Error moving client stats target: {e}")


def remove_client_menu_items():
    """Drop per-client entries (Share/Export/stats target) from the context menu."""
    for i in range(context_menu.index('end'), -1, -1):
        try:
            label = context_menu.entrycget(i, "label")
        except tk.TclError:
            continue
        if (
            label.startswith("Share ")
            or label == "Export to PDB"
            or label.endswith("Alarm on change")
            or label in (STATS_TO_MAIN_LABEL, STATS_TO_FINALIZATION_LABEL)
        ):
            context_menu.delete(i)
    if speed_boost is not None:
        speed_boost.remove_client_menu_items(context_menu)
    remove_leading_context_separator()


def toggle_alarm_on_change(pid):
    """Arm/disarm the one-shot score-change alarm for a single client row."""
    if pid is None:
        return
    state = monitored_processes[pid]
    state['alarm_on_change'] = not state.get('alarm_on_change', False)


def show_tree_context_menu(event):
    """RMB on a client row: context menu extended with that client's stats and save
    actions (move between the Main and Finalization stats tables, Share, Export)."""
    item = process_tree.identify('item', event.x, event.y)
    if not item:
        return  # empty area: the root binding shows the plain global menu
    tags = process_tree.item(item, 'tags')
    folder = get_folder_tag(tags)
    if not folder:
        return

    remove_client_menu_items()
    insert_index = 0

    pid = get_pid_tag(tags)
    if pid is not None:
        alarm_armed = bool(monitored_processes.get(pid, {}).get('alarm_on_change'))
        context_menu.insert(insert_index, "command",
            label=("✓ Alarm on change" if alarm_armed else "Alarm on change"),
            command=lambda p=pid: toggle_alarm_on_change(p),
        )
        insert_index += 1
        if speed_boost is not None:
            insert_index = speed_boost.insert_client_menu_item(context_menu, insert_index, pid, folder)

    # Stats: move this client between the Main (vertical) and Finalization
    # (horizontal) tables, depending on where it currently is.
    identity = get_client_stats_identity(item)
    if identity is not None:
        client_name, puzzle_id = identity
        current_target = stats_manager.get_active_targets(puzzle_id).get(client_name, "vertical")
        if current_target == "horizontal":
            target_label, new_target = STATS_TO_MAIN_LABEL, "vertical"
        else:
            target_label, new_target = STATS_TO_FINALIZATION_LABEL, "horizontal"
        context_menu.insert(insert_index, "command",
            label=target_label,
            command=lambda p=puzzle_id, c=client_name, t=new_target: move_client_stats_target(p, c, t),
        )
        insert_index += 1

    info = get_share_info(folder)
    save_path = info.get('path')
    if not (info.get('done') and not save_path):  # skip Share/Export only when this client has no saves
        save_name = info.get('name')
        save_score = info.get('score')
        if save_name and save_score:
            share_label = f"Share {save_name} {save_score}"
        elif save_path:
            share_label = f"Share {os.path.basename(save_path)}"
        else:
            share_label = "Share SaveFile"
        context_menu.insert(insert_index, "command",
            label=share_label,
            command=lambda f=folder: copy_to_all_clients(f)
        )
        insert_index += 1
        if save_path and save_name and export_pdb is not None:
            puzzle_id_for_pdb = get_puzzle_id(get_pid_tag(tags))
            context_menu.insert(insert_index, "command",
                label="Export to PDB",
                command=lambda path=save_path, name=save_name, pid=puzzle_id_for_pdb: export_save_to_pdb(path, name, pid)
            )
        else:
            context_menu.insert(insert_index, "command", label="Export to PDB", state="disabled")
        insert_index += 1

    if insert_index > 0:
        context_menu.insert(insert_index, "separator")

    post_context_menu(event)
    return "break"


def post_context_menu(event):
    if palette_var is not None:
        palette_var.set(settings_manager.ACTIVE_DISPLAY_PALETTE)
    context_menu.post(event.x_root, event.y_root)


def show_context_menu(event):
    """RMB outside client rows: global menu without per-client items."""
    remove_client_menu_items()
    post_context_menu(event)

def reset_window_size():
    """Reset window size and enable auto-size"""
    root.size_locked = False
    adjust_window_size(force=True)
    
    # Remove the menu item
    try:
        for i in range(context_menu.index("end") + 1):
            try:
                if context_menu.entrycget(i, "label") == "Reset window size":
                    context_menu.delete(i)
                    break
            except:
                continue
    except Exception as e:
        print(f"Error removing menu item in reset_window_size: {e}")
def on_window_resize(event):
    """Handler for window resize"""
    if not hasattr(root, 'resizing'):
        root.resizing = False
    
    if event.widget == root and not root.resizing:
        root.resizing = True
        
        # Only lock size if it's not a programmatic resize
        if not getattr(root, 'programmatic_resize', False):
            # Check if the size was changed by the user
            if not hasattr(root, 'last_auto_size'):
                root.last_auto_size = (root.winfo_width(), root.winfo_height())
            current_size = (event.width, event.height)
            
            if current_size != root.last_auto_size:
                # The size was changed by the user
                if not hasattr(root, 'size_locked') or not root.size_locked:
                    root.size_locked = True
                    # Add menu item after "Always on Top"
                    always_top_index = None
                    for i in range(context_menu.index("end") + 1):
                        try:
                            if context_menu.entrycget(i, "label").endswith("Always on Top"):
                                always_top_index = i
                                break
                        except:
                            continue
                    
                    if always_top_index is not None:
                        # Check if the menu item already exists
                        menu_exists = False
                        for i in range(context_menu.index("end") + 1):
                            try:
                                if context_menu.entrycget(i, "label") == "Reset window size":
                                    menu_exists = True
                                    break
                            except:
                                continue
                        
                        if not menu_exists:
                            context_menu.insert(always_top_index + 1, "command",
                                label="Reset window size",
                                command=reset_window_size
                            )
        
        root.resizing = False

def on_treeview_click(event):
    """Handle the event when an item in the treeview is double-clicked."""
    global last_double_click_time
    last_double_click_time = time.time()
    column = process_tree.identify_column(event.x)
    item = process_tree.identify('item', event.x, event.y)
    
    # Get all tags and extract only pid and folder_path
    tags = process_tree.item(item, 'tags')
    # Find pid (first tag) and folder_path (second tag)
    pid = next(tag for tag in tags if tag.isdigit())
    folder_path = next(tag for tag in tags if '\\' in tag or '/' in tag)
    
    if column == "#1" or column == "#4":
        foldit_log_handler.export_log(
            folder_path,
            open_file=True,
            puzzle_id=get_puzzle_id(int(pid)),
        )
    if column == "#2":
        pid_int = int(pid)
        if speed_boost is not None:
            if speed_boost.before_activate(
                pid_int,
                after=lambda target_pid=pid_int: window_manager.activate_client(target_pid),
            ):
                return
        window_manager.activate_client(pid_int)
    if column == "#3":
        open_folder(folder_path)

def find_foldit_installations():
    """Return (installation folders, running folders); (None, None) if none can be found."""
    parent_dirs = get_running_foldit_parent_dirs()
    if not parent_dirs:
        remembered_parent = str(settings_manager.LAST_SEEN_FOLDIT_PARENT).strip()
        if not remembered_parent or not os.path.isdir(remembered_parent):
            messagebox.showinfo("Info", "No running Foldit clients found. Start one manually first.")
            return None, None
        parent_dirs = [remembered_parent]

    if not window_manager.get_executable_name():
        messagebox.showerror("Error", "Unsupported operating system")
        return None, None
    foldit_folders = window_manager.find_installation_folders(parent_dirs)
    if not foldit_folders:
        messagebox.showerror("Error", "No Foldit installations found")
        return None, None
    foldit_folders.sort(key=lambda x: natural_sort(os.path.basename(x)))

    running_folders = set()
    for item in process_tree.get_children():
        folder = get_folder_tag(process_tree.item(item, 'tags'))
        if folder:
            running_folders.add(folder)
    return foldit_folders, running_folders


def launch_client_folder(folder_path):
    """Launch a Foldit client from a specific installation folder."""
    try:
        window_manager.launch_client(folder_path)
        settings_manager.save_last_seen_foldit_parent(get_foldit_parent_dir(folder_path))
    except Exception as e:
        messagebox.showerror("Error", f"Failed to launch Foldit: {str(e)}")


def launch_next_client():
    """Launch the first available Foldit client."""
    foldit_folders, running_folders = find_foldit_installations()
    if not foldit_folders:
        return

    # Launch the lowest available installation, regardless of which client is running now.
    for current_folder in foldit_folders:
        if current_folder not in running_folders:
            launch_client_folder(current_folder)
            return

    messagebox.showinfo("Info", "No available Foldit clients found")


def show_new_client_menu(event):
    """RMB on New Client: menu of all known installations; running ones are checked off."""
    foldit_folders, running_folders = find_foldit_installations()
    if not foldit_folders:
        return "break"

    menu = tk.Menu(root, tearoff=0)
    for folder in foldit_folders:
        name = os.path.basename(folder)
        if folder in running_folders:
            menu.add_command(label=f"✓ {name}", state="disabled")
        else:
            menu.add_command(label=name, command=lambda f=folder: launch_client_folder(f))
    try:
        menu.tk_popup(event.x_root, event.y_root)
    finally:
        menu.grab_release()
    return "break"
        
def handle_root_configure(event=None):
    on_window_resize(event)

#--------------------------------------------------------------------------------------------MAIN WINDOW GUI
# Create a global instance of WindowManager
window_manager = WindowManager()

root = tk.Tk()
root.title("Foldit Monitor")
#root.overrideredirect(True)  # Remove window title and control buttons

icon_data = create_ribbon_icon()
icon = tk.PhotoImage(data=icon_data)
root.iconphoto(True, icon)

# Initialize sound for the alert. Create sound file if it doesn't exist.
def init_sound():
    try:
        import pygame
        pygame.mixer.init()
    except pygame.error as e:
        print(f"Error initializing sound: {e}")
        return False
# init_sound()
#if os.path.exists(settings_manager.sound_file):
#   pygame.mixer.music.load(settings_manager.sound_file)
#else:
#    alert_buffer = create_alert_sound(duration=2)
#    with open(settings_manager.sound_file, 'wb') as f:
#        f.write(alert_buffer.getvalue())
#pygame.mixer.music.load(settings_manager.sound_file)
#pygame.mixer.music.set_volume(settings_manager.VOLUME)


def play_alert_sound():
    """(Re)initialize mixer so playback goes to the current default device, then play."""
    try:
        import pygame  # lazy: keep the ~1s pygame/SDL import off the startup path
        pygame.mixer.quit()
        pygame.mixer.init()
        pygame.mixer.music.load(settings_manager.sound_file)
        pygame.mixer.music.set_volume(settings_manager.VOLUME)
        pygame.mixer.music.play()
    except pygame.error as e:
        print(f"Error playing sound: {e}")


# Set window position and make draggable
if settings_x == -1 or settings_y == -1:
    # Legacy configs used -1 as an unset position marker.
    x_position = 1
    y_position = 1
else:
    x_position = settings_x
    y_position = settings_y
root.geometry(f"{window_width}x{window_height}+{x_position}+{y_position}")
make_window_draggable(root)
root.resizing = False



process_tree = ttk.Treeview(root, 
    columns=("Score", "CPU", "Folder", "Type", "Puzzle") if settings_manager.settings['display']['show_puzzle_column'] else ("Score", "CPU", "Folder", "Type"), 
    show="headings", 
    selectmode='none'
    )
process_tree.heading("Score", text="Score")
process_tree.heading("CPU", text="CPU", anchor="center")
process_tree.heading("Folder", text="Cl")
process_tree.heading("Type", text="Script")
process_tree.column("CPU", anchor="center")
if settings_manager.settings['display']['show_puzzle_column']:
    process_tree.heading("Puzzle", text="Puzzle")
    process_tree.column("Puzzle", width=50, stretch=False)
process_tree.pack(fill="both", expand=True)
speed_boost = FolditSpeedBoostIntegration(
    root,
    process_tree,
    get_pid_tag,
)


# Create a style for small buttons
style = ttk.Style()
style.configure('Small.TButton', padding=1)

# Create a frame for buttons at the bottom of the window
button_frame = ttk.Frame(root)
button_frame.pack(side="bottom", fill="x", padx=2, pady=(0, 2))

# Create buttons with reduced size
style.configure('Small.TButton', padding=0, font=('TkDefaultFont', 7))
new_client_button = ttk.Button(button_frame, text="New Client", command=launch_next_client,
          width=9, style='Small.TButton')
new_client_button.pack(side="left", padx=0)
new_client_button.bind("<Button-3>", show_new_client_menu)
stats_button = ttk.Button(
    button_frame,
    text="Stats",
    command=toggle_stats_window,
    width=8,
    style='Small.TButton',
)
stats_button.pack(side="left", padx=0)
stats_button.bind("<Button-3>", show_stats_puzzle_menu)
stats_puzzle_menu = tk.Menu(root, tearoff=0)
logs_menu = tk.Menu(root, tearoff=0)
logs_menu.add_command(label="Open all local logs", command=open_all_logs)
logs_menu.add_command(label="Dump all local logs", command=dump_all_logs)
logs_menu.add_separator()
logs_menu.add_command(label="Open all remote logs", command=open_all_remote_logs)
logs_menu.add_command(label="Dump all remote logs", command=dump_all_remote_logs)
all_logs_button = ttk.Button(button_frame, text="All Logs", command=open_all_logs,
          width=8, style='Small.TButton')
all_logs_button.pack(side="left", padx=0)
all_logs_button.bind("<Button-3>", show_logs_menu)
# ttk.Button(button_frame, text="Dump Logs", command=dump_all_logs,
#           width=8, style='Small.TButton').pack(side="left", padx=0)
# ttk.Button(button_frame, text="—", command=minimize_window,
#           width=1, style='Small.TButton').pack(side="right", padx=0)
# ttk.Button(button_frame, text="×", command=close_app,
#           width=1, style='Small.TButton').pack(side="right", padx=0)


root.protocol("WM_DELETE_WINDOW", on_close)

# Create the Connect button context menu
connect_menu = tk.Menu(root, tearoff=0)
connect_menu.add_command(label="Connect...", command=show_connect_dialog)
connect_menu.add_separator()
connect_menu.add_command(label="Disconnect all", command=disconnect_all_clients)

# Create the context menu
context_menu = tk.Menu(root, tearoff=0)
palette_var = tk.StringVar(value=settings_manager.ACTIVE_DISPLAY_PALETTE)
palette_menu = tk.Menu(context_menu, tearoff=0)
for palette_name in settings_manager.DISPLAY_PALETTES:
    palette_menu.add_radiobutton(
        label=palette_name.title(),
        value=palette_name,
        variable=palette_var,
        command=lambda name=palette_name: apply_display_palette(name),
    )
all_clients_menu = tk.Menu(context_menu, tearoff=0)
if speed_boost is not None:
    speed_boost.add_global_menu_items(all_clients_menu)
    context_menu.add_cascade(label="All clients", menu=all_clients_menu)
    context_menu.add_separator()
context_menu.add_cascade(label="Palette", menu=palette_menu)
context_menu.add_command(label="Always on Top", command=toggle_always_on_top)
context_menu.add_command(label="Close", command=on_close)


# Bind events for window state changes
root.bind("<Map>", on_window_state_change)    # Restored
# Bind right-click to show the context menu
root.bind("<Button-3>", show_context_menu)
# Bind the treeview click event to the on_treeview_click function
process_tree.bind('<Double-1>', on_treeview_click)

# In the process_tree creation section, add event bindings:
process_tree.bind('<Button-2>', handle_middle_click)
process_tree.bind('<Button-3>', show_tree_context_menu)
root.bind("<Configure>", handle_root_configure)
root.bind('<FocusOut>', on_focus_out)

init_fonts()
root.tooltip = setup_tooltip(root)

network_manager = NetworkManager(
    main_window=root,
    callbacks={
        'create_remote_tree': create_remote_tree,
        'remove_remote_tree': remove_remote_tree,
        'update_remote_trees': update_remote_trees,
        'build_artifact': build_remote_artifact,
        'build_artifact_query': build_remote_artifact_query,
        'artifact_received': handle_remote_artifact_received,
        'artifact_error': handle_remote_artifact_error,
    },
    tree=process_tree,  # Добавляем process_tree
    monitored_processes=monitored_processes,  # Добавляем monitored_processes
    password=settings_manager.NETWORK_PASSWORD,
    artifact_root=os.path.join(stats_manager.logs_folder, "_remote"),
    max_artifact_bytes=settings_manager.NETWORK_MAX_ARTIFACT_BYTES,
    artifact_chunk_bytes=settings_manager.NETWORK_ARTIFACT_CHUNK_BYTES,
)
network_manager.start_server(settings_manager.DEFAULT_PORT, settings_manager.SERVER_TIMEOUT)
connect_button = ttk.Button(button_frame, text="Connect", command=show_connect_dialog,
            width=9, style='Small.TButton')
connect_button.pack(side="left", padx=0)
connect_button.bind("<Button-3>", show_connect_menu)
refresh_stats_puzzle_menu()
#--------------------------Main loop

# Start the periodic update scheduling
schedule_update()

# Add automatic connections at startup
connect_to_startup_list()

# Start the gentle auto-reconnect sweep for kept-alive hosts. Delay the first pass by
# one interval so the startup connections above have time to land (avoids double-dialing).
root.after(RECONNECT_INTERVAL * 1000, schedule_reconnect)

root.mainloop()

# if __name__ == "__main__":
#     # Start the periodic update scheduling
#     schedule_update()

#     # Add automatic connections at startup
#     connect_to_startup_list()

#     root.mainloop()
