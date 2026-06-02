from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Tuple

from .models import Params


def rotate_point(x: float, y: float, angle_deg: float) -> Tuple[float, float]:
    angle = math.radians(angle_deg)
    c = math.cos(angle)
    s = math.sin(angle)
    return x * c - y * s, x * s + y * c


def fit_label_font_size(text: str, board_w: float, board_h: float, params: Params) -> float:
    """Estimate a font size that stays inside the board footprint."""
    label = text.strip() or "PCB"
    max_w = board_w * params.label_margin_ratio
    max_h = board_h * params.label_height_ratio
    chars = max(1, len(label))

    width_limited = max_w / (params.label_char_width_factor * chars)
    height_limited = max_h
    size = min(width_limited, height_limited)
    return max(1.8, size)


def _chunk_text(text: str, limit: int) -> List[str]:
    if limit <= 0:
        return [text]
    return [text[i : i + limit] for i in range(0, len(text), limit)]


def wrap_label_to_fit(text: str, board_w: float, board_h: float, params: Params) -> Tuple[List[str], float]:
    """Wrap label text and compute a font size that fits within board footprint."""
    label = " ".join((text or "").split()) or "PCB"
    max_w = board_w * params.label_margin_ratio
    max_h = board_h * params.label_height_ratio
    line_gap_ratio = 0.28

    words = label.split(" ")
    total_chars = sum(len(w) for w in words) + max(0, len(words) - 1)

    best_lines = [label]
    best_size = fit_label_font_size(label, board_w, board_h, params)

    max_lines = max(1, min(6, len(words) + 2))
    for line_count in range(1, max_lines + 1):
        target_chars = max(1, math.ceil(total_chars / line_count))
        limit = max(4, int(target_chars * 1.15))

        candidate_lines: List[str] = []
        current = ""
        for word in words:
            parts = _chunk_text(word, limit) if len(word) > limit else [word]
            for part in parts:
                proposal = f"{current} {part}".strip()
                if current and len(proposal) > limit:
                    candidate_lines.append(current)
                    current = part
                else:
                    current = proposal
        if current:
            candidate_lines.append(current)

        if len(candidate_lines) > max_lines:
            continue

        longest = max(len(line) for line in candidate_lines)
        width_limited = max_w / (params.label_char_width_factor * max(1, longest))
        height_units = len(candidate_lines) + line_gap_ratio * max(0, len(candidate_lines) - 1)
        height_limited = max_h / max(1e-6, height_units)
        size = min(width_limited, height_limited)

        if size > best_size:
            best_size = size
            best_lines = candidate_lines

    return best_lines, max(1.8, best_size)


def rotate_side(side: str, angle_deg: float) -> str:
    side_vecs = {
        "x-": (-1.0, 0.0),
        "x+": (1.0, 0.0),
        "y-": (0.0, -1.0),
        "y+": (0.0, 1.0),
    }
    vx, vy = side_vecs.get(side, (-1.0, 0.0))
    rx, ry = rotate_point(vx, vy, angle_deg)
    if abs(rx) >= abs(ry):
        return "x+" if rx >= 0.0 else "x-"
    return "y+" if ry >= 0.0 else "y-"


def _rects_intersect(a: Tuple[float, float, float, float], b: Tuple[float, float, float, float]) -> bool:
    ax0, ax1, ay0, ay1 = a
    bx0, bx1, by0, by1 = b
    return (ax0 < bx1) and (ax1 > bx0) and (ay0 < by1) and (ay1 > by0)


def _axis_void_ranges(inner_min: float, inner_max: float, rib_width: float, rib_pitch: float) -> List[Tuple[float, float]]:
    if inner_max <= inner_min:
        return []

    center_start = inner_min + rib_width / 2.0
    center_end = inner_max - rib_width / 2.0
    if center_end < center_start:
        centers = [(inner_min + inner_max) / 2.0]
    else:
        centers = []
        c = center_start
        while c <= center_end + 1e-6:
            centers.append(c)
            c += rib_pitch
        if not centers:
            centers = [(inner_min + inner_max) / 2.0]

    voids: List[Tuple[float, float]] = []
    cursor = inner_min
    for c in centers:
        left = max(inner_min, c - rib_width / 2.0)
        right = min(inner_max, c + rib_width / 2.0)
        if left - cursor > 0.4:
            voids.append((cursor, left))
        cursor = max(cursor, right)
    if inner_max - cursor > 0.4:
        voids.append((cursor, inner_max))
    return voids


def _compute_board_keep_rect(
    board: Dict[str, Any],
    island_padding_mm: float,
    panel_w: float,
    panel_h: float,
) -> Optional[Tuple[float, float, float, float]]:
    try:
        cx = float(board.get("x_mm", 0.0))
        cy = float(board.get("y_mm", 0.0))
        bw = float(board.get("board_w_mm", 0.0))
        bh = float(board.get("board_h_mm", 0.0))
        rot = math.radians(float(board.get("rotation_deg", 0.0)))
    except Exception:
        return None

    if bw <= 0.0 or bh <= 0.0:
        return None

    footprint_w = abs(bw * math.cos(rot)) + abs(bh * math.sin(rot))
    footprint_h = abs(bh * math.cos(rot)) + abs(bw * math.sin(rot))

    x0 = max(0.0, cx - footprint_w / 2.0 - island_padding_mm)
    x1 = min(panel_w, cx + footprint_w / 2.0 + island_padding_mm)
    y0 = max(0.0, cy - footprint_h / 2.0 - island_padding_mm)
    y1 = min(panel_h, cy + footprint_h / 2.0 + island_padding_mm)
    if x1 <= x0 or y1 <= y0:
        return None
    return (x0, x1, y0, y1)


def _make_ribbed_cut_cells(
    panel_w: float,
    panel_h: float,
    frame_mm: float,
    rib_width_mm: float,
    rib_pitch_mm: float,
    keep_rects: List[Tuple[float, float, float, float]],
) -> List[Tuple[float, float, float, float]]:
    inner_x0 = frame_mm
    inner_x1 = panel_w - frame_mm
    inner_y0 = frame_mm
    inner_y1 = panel_h - frame_mm
    if inner_x1 <= inner_x0 or inner_y1 <= inner_y0:
        return []

    x_voids = _axis_void_ranges(inner_x0, inner_x1, rib_width_mm, rib_pitch_mm)
    y_voids = _axis_void_ranges(inner_y0, inner_y1, rib_width_mm, rib_pitch_mm)

    cells: List[Tuple[float, float, float, float]] = []
    for x0, x1 in x_voids:
        for y0, y1 in y_voids:
            rect = (x0, x1, y0, y1)
            if any(_rects_intersect(rect, keep) for keep in keep_rects):
                continue
            cells.append(rect)
    return cells
