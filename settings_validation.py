"""Validation for editable Foldit Monitor settings.

The editor validates a complete effective profile before touching the user file.
Unknown keys are deliberately left alone for forward compatibility.
"""

import os
import ipaddress
import re
from typing import Any


class SettingsValidationError(ValueError):
    pass


def _fail(path: str, message: str) -> None:
    raise SettingsValidationError(f"{path}: {message}")


def _object(value: Any, path: str) -> dict:
    if not isinstance(value, dict):
        _fail(path, "expected an object")
    return value


def _string(value: Any, path: str, *, empty: bool = True) -> None:
    if not isinstance(value, str) or (not empty and not value.strip()):
        _fail(path, "expected a string" + (" with a value" if not empty else ""))


def _number(value: Any, path: str, *, minimum=0, maximum=None, integer=False) -> None:
    kind = int if integer else (int, float)
    if isinstance(value, bool) or not isinstance(value, kind):
        _fail(path, "expected " + ("an integer" if integer else "a number"))
    if value < minimum or (maximum is not None and value > maximum):
        _fail(path, f"expected a value from {minimum} to {maximum if maximum is not None else 'above'}")


def _strings(value: Any, path: str, *, nonempty=False) -> None:
    if not isinstance(value, list) or (nonempty and not value):
        _fail(path, "expected a non-empty list" if nonempty else "expected a list")
    for index, item in enumerate(value):
        _string(item, f"{path}[{index}]", empty=not nonempty)


def _boolean(value: Any, path: str) -> None:
    if not isinstance(value, bool):
        _fail(path, "expected true or false")


def _address(value: Any, path: str) -> None:
    _string(value, path, empty=False)
    if any(character.isspace() for character in value) or "/" in value or "\\" in value:
        _fail(path, "expected a host name or IP address")
    try:
        ipaddress.ip_address(value)
        return
    except ValueError:
        pass
    if len(value) > 253 or not re.fullmatch(r"[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*", value):
        _fail(path, "expected a host name or IP address")


def validate_settings(settings: dict) -> None:
    """Raise SettingsValidationError for the first invalid editable setting."""
    data = _object(settings, "settings")
    _number(data["check_interval"], "check_interval", minimum=1, integer=True)
    _number(data["monitor_duration"], "monitor_duration", minimum=data["check_interval"], integer=True)
    _number(data["high_cpu_threshold"], "high_cpu_threshold", minimum=0, maximum=100)
    _number(data["low_cpu_threshold"], "low_cpu_threshold", minimum=0, maximum=100)
    if data["low_cpu_threshold"] >= data["high_cpu_threshold"]:
        _fail("low_cpu_threshold", "must be below high_cpu_threshold")

    display = _object(data["display"], "display")
    for key in ("tooltip_lines", "stale_tick_limit", "script_type_fallback_max_length"):
        _number(display[key], f"display.{key}", minimum=1, integer=True)
    for key in ("always_on_top", "show_puzzle_column"):
        _boolean(display[key], f"display.{key}")
    if display["stats_ui_backend"] not in ("tk", "pyside6"):
        _fail("display.stats_ui_backend", "expected tk or pyside6")
    fonts = _object(display["fonts"], "display.fonts")
    _string(fonts["family"], "display.fonts.family", empty=False)
    for key in ("normal_size", "tooltip_size", "stats_size"):
        _number(fonts[key], f"display.fonts.{key}", minimum=1, integer=True)
    if display["active_palette"] == "custom":
        appearance = _object(display.get("row_appearance", {}), "display.row_appearance")
        for state, values in appearance.items():
            if state not in {"tree", "normal", "idle", "stale", "fin", "visible", "focused", "copy_source"}:
                _fail(f"display.row_appearance.{state}", "unknown row state")
            values = _object(values, f"display.row_appearance.{state}")
            for field, value in values.items():
                path = f"display.row_appearance.{state}.{field}"
                allowed = {"background", "heading_background", "heading_foreground"} if state == "tree" else {"foreground", "background", "stale_to_foreground", "fade_to_foreground", "bold", "italic"}
                if field not in allowed:
                    _fail(path, "unknown appearance parameter")
                if field in {"bold", "italic"}:
                    _boolean(value, path)
                else:
                    if not isinstance(value, str) or not re.fullmatch(r"#[0-9a-fA-F]{6}", value):
                        _fail(path, "expected a #RRGGBB color")

    network = _object(data["network"], "network")
    _address(network["default_address"], "network.default_address")
    _number(network["default_port"], "network.default_port", minimum=1, maximum=65535, integer=True)
    _number(network["server_timeout"], "network.server_timeout", minimum=1, integer=True)
    _string(network["password"], "network.password", empty=False)
    _boolean(network["auto_reconnect"], "network.auto_reconnect")
    for key in ("max_artifact_bytes", "artifact_chunk_bytes"):
        _number(network[key], f"network.{key}", minimum=1, integer=True)
    if network["artifact_chunk_bytes"] > network["max_artifact_bytes"]:
        _fail("network.artifact_chunk_bytes", "must not exceed max_artifact_bytes")
    connections = network["startup_connections"]
    if not isinstance(connections, list):
        _fail("network.startup_connections", "expected a list")
    for index, connection in enumerate(connections):
        if not isinstance(connection, (list, tuple)) or len(connection) != 2:
            _fail(f"network.startup_connections[{index}]", "expected [address, port]")
        _address(connection[0], f"network.startup_connections[{index}].address")
        _number(connection[1], f"network.startup_connections[{index}].port", minimum=1, maximum=65535, integer=True)

    sound = _object(data["sound"], "sound")
    _string(sound["alert_file"], "sound.alert_file", empty=False)
    _number(sound["volume"], "sound.volume", minimum=0, maximum=1)

    logging = _object(data["logging"], "logging")
    for key in ("max_lines", "max_line_length", "stats_save_interval_minutes"):
        _number(logging[key], f"logging.{key}", minimum=1, integer=True)
    _number(logging["stats_score_decimals"], "logging.stats_score_decimals", minimum=0, maximum=15, integer=True)
    _boolean(logging["managed_log_exports"], "logging.managed_log_exports")
    _string(logging["logs_folder"], "logging.logs_folder", empty=False)
    _strings(logging["exclude_score_strings"], "logging.exclude_score_strings")
    patterns = logging["score_patterns"]
    _strings(patterns, "logging.score_patterns", nonempty=True)
    for index, pattern in enumerate(patterns):
        try:
            re.compile(pattern)
        except re.error as error:
            _fail(f"logging.score_patterns[{index}]", str(error))

    speed = _object(data["speed_boost"], "speed_boost")
    _boolean(speed["enabled"], "speed_boost.enabled")
    profiles = _object(speed["profiles"], "speed_boost.profiles")
    if not profiles or speed["profile"] not in profiles:
        _fail("speed_boost.profile", "must name an available profile")
    for name, profile in profiles.items():
        _string(name, "speed_boost.profiles key", empty=False)
        profile = _object(profile, f"speed_boost.profiles.{name}")
        _string(profile["label"], f"speed_boost.profiles.{name}.label", empty=False)
        for key in ("replacement_sleep_ms", "timer_resolution_ms"):
            _number(profile[key], f"speed_boost.profiles.{name}.{key}", minimum=1, maximum=1000, integer=True)
    offsets = speed["offsets"]
    if not isinstance(offsets, list) or not offsets:
        _fail("speed_boost.offsets", "expected a non-empty list")
    for index, offset in enumerate(offsets):
        if isinstance(offset, bool) or not (isinstance(offset, int) and 0 <= offset <= 0xFFFFFFFF or isinstance(offset, str) and re.fullmatch(r"0[xX][0-9a-fA-F]+", offset.strip()) and int(offset, 16) <= 0xFFFFFFFF):
            _fail(f"speed_boost.offsets[{index}]", "expected a 32-bit hexadecimal offset")

    mapping = _object(data["script_type_mapping"], "script_type_mapping")
    for key, item in mapping.items():
        _string(key, "script_type_mapping key", empty=False)
        item = _object(item, f"script_type_mapping.{key}")
        _string(item["name"], f"script_type_mapping.{key}.name")
        _number(item["column_number"], f"script_type_mapping.{key}.column_number", minimum=0, integer=True)
        rules = item.get("state_snapshot_rules", [])
        if not isinstance(rules, list):
            _fail(f"script_type_mapping.{key}.state_snapshot_rules", "expected a list")
        for index, rule in enumerate(rules):
            path = f"script_type_mapping.{key}.state_snapshot_rules[{index}]"
            rule = _object(rule, path)
            _string(rule.get("name", ""), f"{path}.name")
            _strings(rule.get("detector"), f"{path}.detector", nonempty=True)
            extractors = rule.get("extractors")
            if not isinstance(extractors, list) or not extractors:
                _fail(f"{path}.extractors", "expected a non-empty list")
            names = set()
            for part_index, extractor in enumerate(extractors):
                ep = f"{path}.extractors[{part_index}]"
                extractor = _object(extractor, ep)
                _string(extractor.get("name"), f"{ep}.name", empty=False)
                for field in ("find_after", "find_before"):
                    _string(extractor.get(field, ""), f"{ep}.{field}")
                if extractor["name"] in names:
                    _fail(ep, "duplicate extractor name")
                names.add(extractor["name"])
            stats_mapping = _object(rule.get("stats_mapping", {}), f"{path}.stats_mapping")
            for field in ("script", "score"):
                if field in stats_mapping and stats_mapping[field] not in names:
                    _fail(f"{path}.stats_mapping.{field}", "must name an extractor")

    monitoring = _object(data.get("monitoring", {}), "monitoring")
    fallbacks = monitoring.get("log_fallbacks", [])
    if not isinstance(fallbacks, list):
        _fail("monitoring.log_fallbacks", "expected a list")
    for index, fallback in enumerate(fallbacks):
        path = f"monitoring.log_fallbacks[{index}]"
        fallback = _object(fallback, path)
        log_path = fallback.get("log_path")
        _string(log_path, f"{path}.log_path", empty=False)
        if not os.path.isabs(os.path.expanduser(log_path)) or not re.fullmatch(r"scriptlog\.[^.]+\.xml", os.path.basename(log_path), re.IGNORECASE):
            _fail(f"{path}.log_path", "expected an absolute scriptlog.<track>.xml path")
        for field in ("name", "title_suffix", "puzzle_id"):
            if field in fallback:
                _string(fallback[field], f"{path}.{field}")
