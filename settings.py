import os
import json
import re
from copy import deepcopy
from typing import Dict, Any

class Settings:
    def __init__(self, root_path: str):
        self.root_path = root_path
        self.settings_file = os.path.join(root_path, "Foldit Monitor.json")
        self.user_settings: Dict[str, Any] = {}
        self.non_merged_paths = {('script_type_mapping',)}
        
        # Константы по умолчанию
        self.DEFAULT_PORT = 8000
        self.DEFAULT_ADDRESS = "127.0.0.1"
        self.SERVER_TIMEOUT = 10
        self.NETWORK_PASSWORD = "fold.it"
        self.NETWORK_AUTO_RECONNECT = True
        self.NETWORK_MAX_ARTIFACT_BYTES = 512 * 1024 * 1024
        self.NETWORK_ARTIFACT_CHUNK_BYTES = 256 * 1024
        self.TOOLTIP_LINES = 15
        self.ALWAYS_ON_TOP = False
        self.VOLUME = 1
        self.MAX_LINES = 400
        self.CHECK_INTERVAL = 2
        self.MONITOR_DURATION = 60
        self.HIGH_CPU_THRESHOLD = 60
        self.LOW_CPU_THRESHOLD = 15
        self.SCRIPT_EXCLUSIONS = ["c-w", "other_script_name"]
        self.BACKUP_FOLDER_NAME = "foldit_backup"
        self.SAVE_TO_BACKUP = True
        self.INACTIVE_PROCESS_PREFIX = "- "
        self.STALE_TICK_LIMIT = 100
        self.DISPLAY_COLOR_KEYS = (
            "normal_font_color",
            "idle_font_color",
            "stale_font_color",
            "idle_background_color",
            "mismatch_background_color",
            "selected_source_background_color",
        )
        self.DISPLAY_PALETTE_ALIASES = {
            "warm": "balanced",
            "classic": "balanced",
            "olive": "cool",
            "copper": "contrast",
        }
        self.DISPLAY_PALETTES = {
            "balanced": {
                "normal_font_color": "#000000",
                "idle_font_color": "#6b7280",
                "stale_font_color": "#6f5d4a",
                "idle_background_color": "#f3f4f6",
                "mismatch_background_color": "#fde8e8",
                "selected_source_background_color": "#dbeafe",
            },
            "cool": {
                "normal_font_color": "#000000",
                "idle_font_color": "#64748b",
                "stale_font_color": "#4f6b8a",
                "idle_background_color": "#eef4f8",
                "mismatch_background_color": "#fee2e2",
                "selected_source_background_color": "#d6e9ff",
            },
            "contrast": {
                "normal_font_color": "#000000",
                "idle_font_color": "#4b5563",
                "stale_font_color": "#8a5a3b",
                "idle_background_color": "#e5e7eb",
                "mismatch_background_color": "#f8d2d9",
                "selected_source_background_color": "#bfdbfe",
            },
            "signal": {
                "normal_font_color": "#000000",
                "idle_font_color": "#6b7280",
                "stale_font_color": "#2f6c7a",
                "idle_background_color": "#f0f4f5",
                "mismatch_background_color": "#ffe1d6",
                "selected_source_background_color": "#d7f0ff",
            },
        }
        self.DEFAULT_DISPLAY_PALETTE = "balanced"
        default_palette = self.DISPLAY_PALETTES[self.DEFAULT_DISPLAY_PALETTE]
        self.NORMAL_FONT_COLOR = default_palette["normal_font_color"]
        self.IDLE_FONT_COLOR = default_palette["idle_font_color"]
        self.STALE_FONT_COLOR = default_palette["stale_font_color"]
        self.IDLE_BACKGROUND_COLOR = default_palette["idle_background_color"]
        self.MISMATCH_BACKGROUND_COLOR = default_palette["mismatch_background_color"]
        self.SELECTED_SOURCE_BACKGROUND_COLOR = default_palette["selected_source_background_color"]
        self.IDLE_STALE_BACKGROUND_COLOR = self.IDLE_BACKGROUND_COLOR
        # Backward-compatible aliases for legacy code paths.
        self.INACTIVE_ROW_FOREGROUND = self.IDLE_FONT_COLOR
        self.INACTIVE_ROW_BACKGROUND = self.IDLE_STALE_BACKGROUND_COLOR
        self.MAX_LINE_LENGTH = 100
        self.SCRIPT_TYPE_FALLBACK_MAX_LENGTH = 10
        self.STATS_UI_BACKEND = "tk"
        self.STATS_LAST_PUZZLE = ""

        # Словари и списки по умолчанию
        self.SCRIPT_TYPE_MAPPING = {
            "ideali": {"name": "Microidealize", "column_number": 1},
            "jet": {"name": "JET", "column_number": 1},
            "with cuts": {"name": "Cuts", "column_number": 1},
            "_cut": {"name": "Cuts", "column_number": 1},
            "gab": {"name": "GAB", "column_number": 0},
            "helix": {"name": "Helix", "column_number": 0},
            "cut ": {"name": "c-w", "column_number": 1},
            "worm": {"name": "Worm", "column_number": 1},
            "drw": {"name": "DRW", "column_number": 0},
            "bwp": {"name": "bwp", "column_number": 1},
            "sidechain": {"name": "Sidechain", "column_number": 1},
            "prediction": {"name": "prediction", "column_number": 0},
            "rebuild": {"name": "Rebuild", "column_number": 0},
            "zz1": {"name": "Rebuild_loc", "column_number": 0},
            "defuze": {"name": "Defuze", "column_number": 0},
            "assembly": {"name": "Assembly", "column_number": 0},
            "hinge": {"name": "Hinge", "column_number": 1},
            "quake": {"name": "Quake", "column_number": 0},
            "remix": {"name": "Remix", "column_number": 0},
            "ligand": {"name": "Ligand", "column_number": 0}
        }
        
        self.EXCLUSION_CRITERIA = ["Group rank", "ignore other score string"]
        
        # Score patterns to detect score string in the log file. Checks each of these elements sequentially.
        self.SCORE_PATTERNS = [
            re.compile(r'\b\d{4,6}\.\d+\b'),  # 4-6 digit numbers with any decimal places
            re.compile(r'\b\d{4,6}\b')        # 4-6 digit numbers with no decimal places
            #re.compile(r'\b\d{4,5}\.\d{2,}\b'),  # 4-5 digit numbers with 2+ decimal places
            #re.compile(r'\b\d{4,5}\.\d\b'),      # 4-5 digit numbers with 1 decimal place
        ]

        
        # Загрузка настроек
        self.settings = self.load_settings()
        self.update_globals()

    def get_default_settings(self) -> Dict[str, Any]:
        """Возвращает словарь настроек по умолчанию"""
        return {
            "launch": {
                "last_seen_foldit_parent": ""
            },
            "display": {
                "tooltip_lines": self.TOOLTIP_LINES,
                "always_on_top": self.ALWAYS_ON_TOP,
                "show_puzzle_column": False,
                "window_position": {
                    "x": 1,
                    "y": 1
                },
                "stats_window_position": {
                    "x": -1,
                    "y": -1
                },
                "fonts": {
                    "family": "DejaVu Sans Mono",
                    "normal_size": 9,
                    "tooltip_size": 7,
                    "stats_size": 7
                },
                "active_palette": self.DEFAULT_DISPLAY_PALETTE,
                "stale_tick_limit": self.STALE_TICK_LIMIT,
                "script_type_fallback_max_length": self.SCRIPT_TYPE_FALLBACK_MAX_LENGTH,
                "stats_ui_backend": self.STATS_UI_BACKEND,
                "stats_last_puzzle": self.STATS_LAST_PUZZLE,
            },
            "network": {
                "default_port": self.DEFAULT_PORT,
                "default_address": self.DEFAULT_ADDRESS,
                "server_timeout": self.SERVER_TIMEOUT,
                "password": self.NETWORK_PASSWORD,
                "auto_reconnect": self.NETWORK_AUTO_RECONNECT,
                "max_artifact_bytes": self.NETWORK_MAX_ARTIFACT_BYTES,
                "artifact_chunk_bytes": self.NETWORK_ARTIFACT_CHUNK_BYTES,
                "startup_connections": [],
                "startup_connections_description": '"startup_connections" holds the list of addresses and ports to automatically connect to at startup.'
            },
            "sound": {
                "alert_file": "alert.wav",
                "volume": self.VOLUME
            },
            "logging": {
                "max_lines": self.MAX_LINES,
                "script_exclusions": self.SCRIPT_EXCLUSIONS,
                "exclude_score_strings": self.EXCLUSION_CRITERIA,
                "logs_folder": "puzzle_logs",
                "stats_save_interval_minutes": 30,
                "stats_score_decimals": 0,
                "save_logs_immediately": True,
                "managed_log_exports": True,
                "log_format": {
                    "column_separator": "||",
                    "value_separator": " | "
                },
                "max_line_length": self.MAX_LINE_LENGTH
            },
            "backup": {
                "folder_name": self.BACKUP_FOLDER_NAME,
                "save_to_backup": self.SAVE_TO_BACKUP
            },
            "script_type_mapping": self.SCRIPT_TYPE_MAPPING
        }

    def load_settings(self) -> Dict[str, Any]:
        """Загружает пользовательские настройки и накладывает их на defaults только в памяти."""
        default_settings = self.get_default_settings()
        should_initialize_file = False

        try:
            if os.path.exists(self.settings_file):
                with open(self.settings_file, 'r', encoding='utf-8') as f:
                    settings = json.load(f)

                if not isinstance(settings, dict):
                    raise ValueError("Settings file root must be a JSON object.")

                self.user_settings = settings
            else:
                self.user_settings = deepcopy(default_settings)
                should_initialize_file = True

        except Exception as e:
            print(f"Error handling settings file: {e}")
            self.user_settings = deepcopy(default_settings)
            should_initialize_file = True

        should_initialize_file = self._migrate_display_color_settings() or should_initialize_file
        should_initialize_file = self._migrate_stats_ui_backend_setting() or should_initialize_file

        effective_settings = deepcopy(self.user_settings)
        self._apply_defaults(effective_settings, default_settings)
        self._apply_display_palette(effective_settings)

        if should_initialize_file:
            self._save_user_settings()

        return effective_settings

    def _migrate_display_color_settings(self) -> bool:
        """Normalize row color settings and migrate to palette-based display settings."""
        display_settings = self.user_settings.get("display")
        if not isinstance(display_settings, dict):
            return False

        changed = False
        color_mappings = (
            ("idle_font_color", "inactive_row_foreground", self.IDLE_FONT_COLOR),
            (
                "idle_background_color",
                "inactive_row_background",
                self.IDLE_BACKGROUND_COLOR,
            ),
            (
                "idle_background_color",
                "idle_stale_background_color",
                self.IDLE_BACKGROUND_COLOR,
            ),
        )

        for new_key, legacy_key, default_value in color_mappings:
            if new_key not in display_settings:
                display_settings[new_key] = display_settings.get(legacy_key, default_value)
                changed = True
            if legacy_key in display_settings:
                del display_settings[legacy_key]
                changed = True
        if "highlight_color" in display_settings:
            del display_settings["highlight_color"]
            changed = True

        palette_name = display_settings.get("active_palette")
        if palette_name is None:
            palette_name = self._detect_display_palette(display_settings)
            display_settings["active_palette"] = palette_name
            changed = True
        else:
            normalized_name = self._normalize_display_palette(palette_name)
            if normalized_name != palette_name:
                display_settings["active_palette"] = normalized_name
                changed = True
            palette_name = normalized_name

        if "stale_tick_limit" not in display_settings:
            display_settings["stale_tick_limit"] = self.STALE_TICK_LIMIT
            changed = True

        if palette_name == "custom":
            custom_defaults = self.DISPLAY_PALETTES[self.DEFAULT_DISPLAY_PALETTE]
            for key in self.DISPLAY_COLOR_KEYS:
                if key not in display_settings:
                    display_settings[key] = custom_defaults[key]
                    changed = True
        else:
            for key in self.DISPLAY_COLOR_KEYS:
                if key in display_settings:
                    del display_settings[key]
                    changed = True

        return changed

    def _normalize_display_palette(self, palette_name: Any) -> str:
        palette_text = str(palette_name).strip().lower()
        palette_text = self.DISPLAY_PALETTE_ALIASES.get(palette_text, palette_text)
        if palette_text == "custom":
            return palette_text
        if palette_text in self.DISPLAY_PALETTES:
            return palette_text
        return self.DEFAULT_DISPLAY_PALETTE

    def _detect_display_palette(self, display_settings: Dict[str, Any]) -> str:
        explicit_colors = {
            key: display_settings[key]
            for key in self.DISPLAY_COLOR_KEYS
            if key in display_settings
        }
        if not explicit_colors:
            return self.DEFAULT_DISPLAY_PALETTE

        for palette_name, palette_colors in self.DISPLAY_PALETTES.items():
            if all(explicit_colors.get(key, palette_colors[key]) == palette_colors[key] for key in self.DISPLAY_COLOR_KEYS):
                return palette_name

        return "custom"

    def _apply_display_palette(self, settings_dict: Dict[str, Any]) -> None:
        display_settings = settings_dict.get("display")
        if not isinstance(display_settings, dict):
            return

        palette_name = self._normalize_display_palette(
            display_settings.get("active_palette", self.DEFAULT_DISPLAY_PALETTE)
        )
        display_settings["active_palette"] = palette_name

        if palette_name == "custom":
            palette_colors = {
                key: display_settings.get(key, self.DISPLAY_PALETTES[self.DEFAULT_DISPLAY_PALETTE][key])
                for key in self.DISPLAY_COLOR_KEYS
            }
        else:
            palette_colors = deepcopy(self.DISPLAY_PALETTES[palette_name])

        display_settings.update(palette_colors)

    def _normalize_stats_ui_backend(self, backend_name: Any) -> str:
        backend_text = str(backend_name).strip().lower()
        if backend_text in {"qt", "pyside", "pyside6"}:
            return "pyside6"
        return "tk"

    def _migrate_stats_ui_backend_setting(self) -> bool:
        display_settings = self.user_settings.get("display")
        if not isinstance(display_settings, dict):
            return False

        current_value = display_settings.get("stats_ui_backend")
        normalized_value = self._normalize_stats_ui_backend(current_value)
        if current_value == normalized_value:
            return False

        display_settings["stats_ui_backend"] = normalized_value
        return True

    def _apply_defaults(self, target: Dict[str, Any], defaults: Dict[str, Any], path: tuple[str, ...] = ()) -> Dict[str, Any]:
        """Добавляет только отсутствующие значения по умолчанию, сохраняя пользовательский порядок."""
        for key, default_value in defaults.items():
            current_path = path + (key,)

            if key not in target:
                target[key] = deepcopy(default_value)
                continue

            if current_path in self.non_merged_paths:
                continue

            target_value = target[key]
            if isinstance(target_value, dict) and isinstance(default_value, dict):
                self._apply_defaults(target_value, default_value, current_path)

        return target

    def _set_nested_value(self, settings_dict: Dict[str, Any], path: tuple[str, ...], value: Any):
        """Создает промежуточные словари по необходимости и сохраняет значение по указанному пути."""
        current = settings_dict
        for key in path[:-1]:
            next_value = current.get(key)
            if not isinstance(next_value, dict):
                next_value = {}
                current[key] = next_value
            current = next_value
        current[path[-1]] = value

    def _save_user_settings(self):
        with open(self.settings_file, 'w', encoding='utf-8') as f:
            json.dump(self.user_settings, f, indent=4, ensure_ascii=False)

    def update_globals(self):
        """Обновляет глобальные переменные из загруженных настроек"""
        self.LAST_SEEN_FOLDIT_PARENT = self.settings['launch'].get('last_seen_foldit_parent', '')
        self.DEFAULT_PORT = self.settings['network']['default_port']
        self.DEFAULT_ADDRESS = self.settings['network']['default_address']
        self.SERVER_TIMEOUT = self.settings['network']['server_timeout']
        self.NETWORK_PASSWORD = self.settings['network'].get('password', 'fold.it')
        self.NETWORK_AUTO_RECONNECT = bool(self.settings['network'].get('auto_reconnect', True))
        self.NETWORK_MAX_ARTIFACT_BYTES = int(
            self.settings['network'].get('max_artifact_bytes', 512 * 1024 * 1024)
        )
        self.NETWORK_ARTIFACT_CHUNK_BYTES = int(
            self.settings['network'].get('artifact_chunk_bytes', 256 * 1024)
        )
        self.TOOLTIP_LINES = self.settings['display']['tooltip_lines']
        self.ALWAYS_ON_TOP = self.settings['display']['always_on_top']
        self.VOLUME = self.settings['sound']['volume']
        self.MAX_LINES = self.settings['logging']['max_lines']
        managed_log_exports_value = self.settings['logging'].get('managed_log_exports', True)
        if isinstance(managed_log_exports_value, str):
            self.MANAGED_LOG_EXPORTS = (
                managed_log_exports_value.strip().lower() not in {"0", "false", "no", "off"}
            )
        else:
            self.MANAGED_LOG_EXPORTS = bool(managed_log_exports_value)
        self.SCRIPT_EXCLUSIONS = self.settings['logging']['script_exclusions']
        # self.LOG_FILE = os.path.join(self.root_path, self.settings['logging']['log_file'])
        self.BACKUP_FOLDER_NAME = self.settings['backup']['folder_name']
        self.SAVE_TO_BACKUP = self.settings['backup']['save_to_backup']
        self.SCRIPT_TYPE_MAPPING = self.settings['script_type_mapping']
        self.sound_file = os.path.join(self.root_path, self.settings['sound']['alert_file'])
        self.ACTIVE_DISPLAY_PALETTE = self.settings['display']['active_palette']
        self.NORMAL_FONT_COLOR = self.settings['display']['normal_font_color']
        self.IDLE_FONT_COLOR = self.settings['display']['idle_font_color']
        self.STALE_FONT_COLOR = self.settings['display']['stale_font_color']
        self.IDLE_BACKGROUND_COLOR = self.settings['display']['idle_background_color']
        self.MISMATCH_BACKGROUND_COLOR = self.settings['display']['mismatch_background_color']
        self.SELECTED_SOURCE_BACKGROUND_COLOR = self.settings['display']['selected_source_background_color']
        self.STATS_UI_BACKEND = self.settings['display'].get('stats_ui_backend', self.STATS_UI_BACKEND)
        self.STATS_LAST_PUZZLE = str(self.settings['display'].get('stats_last_puzzle', self.STATS_LAST_PUZZLE)).strip()
        self.IDLE_STALE_BACKGROUND_COLOR = self.IDLE_BACKGROUND_COLOR
        try:
            self.STALE_TICK_LIMIT = max(1, int(self.settings['display'].get('stale_tick_limit', self.STALE_TICK_LIMIT)))
        except (TypeError, ValueError):
            self.STALE_TICK_LIMIT = 100
        self.CHECK_INTERVAL = self.settings.get('check_interval', self.CHECK_INTERVAL)
        self.MONITOR_DURATION = self.settings.get('monitor_duration', self.MONITOR_DURATION)
        self.HIGH_CPU_THRESHOLD = self.settings.get('high_cpu_threshold', self.HIGH_CPU_THRESHOLD)
        self.LOW_CPU_THRESHOLD = self.settings.get('low_cpu_threshold', self.LOW_CPU_THRESHOLD)
        self.INACTIVE_PROCESS_PREFIX = self.settings.get('inactive_process_prefix', self.INACTIVE_PROCESS_PREFIX)
        self.INACTIVE_ROW_FOREGROUND = self.IDLE_FONT_COLOR
        self.INACTIVE_ROW_BACKGROUND = self.IDLE_STALE_BACKGROUND_COLOR
        self.MAX_LINE_LENGTH = self.settings['logging']['max_line_length']

        # Lower-case aliases kept for legacy callers.
        self.default_port = self.DEFAULT_PORT
        self.default_address = self.DEFAULT_ADDRESS
        self.server_timeout = self.SERVER_TIMEOUT
        self.network_max_artifact_bytes = self.NETWORK_MAX_ARTIFACT_BYTES
        self.network_artifact_chunk_bytes = self.NETWORK_ARTIFACT_CHUNK_BYTES
        self.tooltip_lines = self.TOOLTIP_LINES
        self.always_on_top = self.ALWAYS_ON_TOP
        self.volume = self.VOLUME
        self.max_lines = self.MAX_LINES
        self.managed_log_exports = self.MANAGED_LOG_EXPORTS
        self.script_exclusions = self.SCRIPT_EXCLUSIONS
        self.backup_folder_name = self.BACKUP_FOLDER_NAME
        self.save_to_backup = self.SAVE_TO_BACKUP
        self.script_type_mapping = self.SCRIPT_TYPE_MAPPING
        self.active_display_palette = self.ACTIVE_DISPLAY_PALETTE
        self.normal_font_color = self.NORMAL_FONT_COLOR
        self.idle_font_color = self.IDLE_FONT_COLOR
        self.stale_font_color = self.STALE_FONT_COLOR
        self.idle_background_color = self.IDLE_BACKGROUND_COLOR
        self.mismatch_background_color = self.MISMATCH_BACKGROUND_COLOR
        self.selected_source_background_color = self.SELECTED_SOURCE_BACKGROUND_COLOR
        self.stats_ui_backend = self.STATS_UI_BACKEND
        self.idle_stale_background_color = self.IDLE_STALE_BACKGROUND_COLOR
        self.stale_tick_limit = self.STALE_TICK_LIMIT
        self.inactive_row_foreground = self.INACTIVE_ROW_FOREGROUND
        self.inactive_row_background = self.INACTIVE_ROW_BACKGROUND
        self.check_interval = self.CHECK_INTERVAL
        self.monitor_duration = self.MONITOR_DURATION
        self.high_cpu_threshold = self.HIGH_CPU_THRESHOLD
        self.low_cpu_threshold = self.LOW_CPU_THRESHOLD
        self.inactive_process_prefix = self.INACTIVE_PROCESS_PREFIX
        self.last_seen_foldit_parent = self.LAST_SEEN_FOLDIT_PARENT

    def save_window_position(self, x: int, y: int):
        """Сохраняет позицию окна в настройках"""
        self._set_nested_value(self.settings, ('display', 'window_position', 'x'), x)
        self._set_nested_value(self.settings, ('display', 'window_position', 'y'), y)
        self._set_nested_value(self.user_settings, ('display', 'window_position', 'x'), x)
        self._set_nested_value(self.user_settings, ('display', 'window_position', 'y'), y)
        self._save_user_settings()

    def save_stats_window_position(self, x: int, y: int):
        """Saves stats window position in settings."""
        self._set_nested_value(self.settings, ('display', 'stats_window_position', 'x'), x)
        self._set_nested_value(self.settings, ('display', 'stats_window_position', 'y'), y)
        self._set_nested_value(self.user_settings, ('display', 'stats_window_position', 'x'), x)
        self._set_nested_value(self.user_settings, ('display', 'stats_window_position', 'y'), y)
        self._save_user_settings()

    def save_active_display_palette(self, palette_name: str):
        """Persist the selected display palette and refresh effective settings."""
        normalized_name = self._normalize_display_palette(palette_name)
        self._set_nested_value(self.user_settings, ('display', 'active_palette'), normalized_name)
        if normalized_name != "custom":
            display_settings = self.user_settings.get('display', {})
            if isinstance(display_settings, dict):
                for key in self.DISPLAY_COLOR_KEYS:
                    display_settings.pop(key, None)

        self.settings = deepcopy(self.user_settings)
        self._apply_defaults(self.settings, self.get_default_settings())
        self._apply_display_palette(self.settings)
        self.update_globals()
        self._save_user_settings()

    def save_last_seen_foldit_parent(self, folder_path: str):
        """Persist the last known parent directory that contains Foldit clients."""
        clean_path = str(folder_path).strip()
        current_value = str(self.settings.get('launch', {}).get('last_seen_foldit_parent', '')).strip()
        if clean_path == current_value:
            return

        self._set_nested_value(self.settings, ('launch', 'last_seen_foldit_parent'), clean_path)
        self._set_nested_value(self.user_settings, ('launch', 'last_seen_foldit_parent'), clean_path)
        self.LAST_SEEN_FOLDIT_PARENT = clean_path
        self.last_seen_foldit_parent = clean_path
        self._save_user_settings()

    def save_stats_score_decimals(self, decimals: int):
        """Persist the score precision used by the stats window."""
        clean_decimals = max(0, int(decimals))
        self._set_nested_value(self.settings, ('logging', 'stats_score_decimals'), clean_decimals)
        self._set_nested_value(self.user_settings, ('logging', 'stats_score_decimals'), clean_decimals)
        self._save_user_settings()

    def save_stats_last_puzzle(self, puzzle_id: str):
        """Persist the puzzle last shown in the stats window."""
        clean_puzzle_id = str(puzzle_id).strip()
        if clean_puzzle_id == self.STATS_LAST_PUZZLE:
            return
        self._set_nested_value(self.settings, ('display', 'stats_last_puzzle'), clean_puzzle_id)
        self._set_nested_value(self.user_settings, ('display', 'stats_last_puzzle'), clean_puzzle_id)
        self.STATS_LAST_PUZZLE = clean_puzzle_id
        self._save_user_settings()

    def save_network_auto_reconnect(self, value: bool):
        """Persist the default 'auto-reconnect' choice from the Connect dialog."""
        clean_value = bool(value)
        if clean_value == self.NETWORK_AUTO_RECONNECT:
            return
        self.NETWORK_AUTO_RECONNECT = clean_value
        self._set_nested_value(self.settings, ('network', 'auto_reconnect'), clean_value)
        self._set_nested_value(self.user_settings, ('network', 'auto_reconnect'), clean_value)
        self._save_user_settings()
