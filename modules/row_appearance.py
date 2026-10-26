"""Palette-driven visual state resolution for monitor rows.

This module deliberately has no Tk dependency.  The monitor and remote tree use
the same resolved appearance tag, while the palette stays in ``settings.py``.
"""

import re
from typing import Any, Mapping, Optional, Tuple


ROW_APPEARANCE_TAG_PREFIX = "appearance_"
STALE_FADE_STEPS = 8

BACKGROUND_PRIORITY = (
    "copy_source",
    "focused",
    "visible",
    "fin",
    "stale",
    "idle",
    "normal",
)
FOREGROUND_OVERRIDE_PRIORITY = ("copy_source", "focused", "visible")
FONT_STATE_ORDER = (
    "normal",
    "idle",
    "stale",
    "fin",
    "visible",
    "focused",
    "copy_source",
)


def clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))


def parse_hex_color(color_value: Any) -> Optional[Tuple[int, int, int]]:
    clean = str(color_value).strip()
    if not re.fullmatch(r"#[0-9a-fA-F]{6}", clean):
        return None
    return tuple(int(clean[index:index + 2], 16) for index in (1, 3, 5))


def blend_hex_colors(start_color: Any, end_color: Any, ratio: float) -> str:
    start_rgb = parse_hex_color(start_color)
    end_rgb = parse_hex_color(end_color)
    if start_rgb is None or end_rgb is None:
        return str(end_color) if ratio >= 1.0 else str(start_color)

    bounded_ratio = clamp(float(ratio), 0.0, 1.0)
    channels = [
        round(start + (end - start) * bounded_ratio)
        for start, end in zip(start_rgb, end_rgb)
    ]
    return "#{:02x}{:02x}{:02x}".format(*channels)


def get_score_fade_ratio(stale_ticks: Any, tick_limit: Any, steps: int = STALE_FADE_STEPS) -> float:
    try:
        ticks = max(0, int(stale_ticks or 0))
    except (TypeError, ValueError):
        ticks = 0
    try:
        normalized_limit = max(1, int(tick_limit or 1))
    except (TypeError, ValueError):
        normalized_limit = 1

    ratio = clamp(ticks / normalized_limit, 0.0, 1.0)
    return round(ratio * max(1, int(steps))) / max(1, int(steps))


def is_row_appearance_tag(tag: Any) -> bool:
    return isinstance(tag, str) and tag.startswith(ROW_APPEARANCE_TAG_PREFIX)


def encode_appearance_color(color_value: Any) -> str:
    if not color_value:
        return "none"
    return str(color_value).strip().lower().lstrip("#")


def decode_appearance_color(token: Any) -> Optional[str]:
    if not token or token == "none":
        return None
    text = str(token).strip()
    if len(text) == 6 and all(character in "0123456789abcdefABCDEF" for character in text):
        return f"#{text.lower()}"
    return text


def build_appearance_tag(font_key: str, foreground: Any, background: Any) -> str:
    return (
        f"{ROW_APPEARANCE_TAG_PREFIX}"
        f"{font_key}|{encode_appearance_color(foreground)}|{encode_appearance_color(background)}"
    )


def parse_appearance_tag(tag: Any) -> Optional[Tuple[str, Optional[str], Optional[str]]]:
    if not is_row_appearance_tag(tag):
        return None
    payload = str(tag)[len(ROW_APPEARANCE_TAG_PREFIX):]
    parts = payload.split("|", 2)
    if len(parts) != 3:
        return None
    font_key, foreground_token, background_token = parts
    return font_key, decode_appearance_color(foreground_token), decode_appearance_color(background_token)


def build_row_visual_state(
    *,
    is_idle: bool = False,
    is_visible: bool = False,
    is_focused: bool = False,
    is_fin: bool = False,
    is_copy_source: bool = False,
    stale_ticks: Any = 0,
) -> dict[str, Any]:
    try:
        normalized_stale_ticks = max(0, int(stale_ticks or 0))
    except (TypeError, ValueError):
        normalized_stale_ticks = 0

    return {
        "normal": True,
        "idle": bool(is_idle),
        "stale": normalized_stale_ticks > 0,
        "fin": bool(is_fin),
        "visible": bool(is_visible),
        "focused": bool(is_focused),
        "copy_source": bool(is_copy_source),
        "stale_ticks": normalized_stale_ticks,
    }


def get_appearance_profile(appearance_profiles: Mapping[str, Any], state_name: str) -> Mapping[str, Any]:
    profile = appearance_profiles.get(state_name, {})
    return profile if isinstance(profile, Mapping) else {}


def resolve_row_appearance(
    row_state: Mapping[str, Any],
    appearance_profiles: Mapping[str, Any],
    stale_tick_limit: Any,
) -> Tuple[str, str, Optional[str]]:
    """Return the font variant, foreground, and background for a row.

    ``fin`` selects a foreground role.  ``stale`` is a phase that fades the
    role's own colour toward its own endpoint, so the two signals never erase
    each other.
    """
    normal_profile = get_appearance_profile(appearance_profiles, "normal")
    normal_foreground = str(normal_profile.get("foreground") or "#000000")
    foreground_role = "fin" if row_state.get("fin") else "normal"
    role_profile = get_appearance_profile(appearance_profiles, foreground_role)
    foreground = str(role_profile.get("foreground") or normal_foreground)

    if row_state.get("stale"):
        stale_profile = get_appearance_profile(appearance_profiles, "stale")
        stale_target = (
            role_profile.get("stale_to_foreground")
            or stale_profile.get("fade_to_foreground")
        )
        if stale_target:
            foreground = blend_hex_colors(
                foreground,
                stale_target,
                get_score_fade_ratio(row_state.get("stale_ticks", 0), stale_tick_limit),
            )

    idle_foreground = get_appearance_profile(appearance_profiles, "idle").get("foreground")
    if row_state.get("idle") and idle_foreground:
        foreground = str(idle_foreground)
    else:
        for state_name in FOREGROUND_OVERRIDE_PRIORITY:
            if not row_state.get(state_name):
                continue
            state_foreground = get_appearance_profile(appearance_profiles, state_name).get("foreground")
            if state_foreground:
                foreground = str(state_foreground)
                break

    background = None
    for state_name in BACKGROUND_PRIORITY:
        if not row_state.get(state_name):
            continue
        candidate = get_appearance_profile(appearance_profiles, state_name).get("background")
        if candidate:
            background = str(candidate)
            break

    bold = any(
        row_state.get(state_name)
        and get_appearance_profile(appearance_profiles, state_name).get("bold")
        for state_name in FONT_STATE_ORDER
    )
    italic = any(
        row_state.get(state_name)
        and get_appearance_profile(appearance_profiles, state_name).get("italic")
        for state_name in FONT_STATE_ORDER
    )
    font_key = "bold_italic" if bold and italic else "bold" if bold else "italic" if italic else "normal"
    return font_key, foreground, background
