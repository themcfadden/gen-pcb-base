#!/usr/bin/env python3

"""Generate an enclosure STL from a PCB STEP model using CadQuery.

Features:
- Reads PCB STEP file from command line.
- Detects likely PCB mounting holes and creates matching standoffs.
- Sets wall height to 5 mm above the tallest component.
- Detects edge connector solids and creates wall cutouts.
"""

from __future__ import annotations

import argparse
import csv
import math
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import cadquery as cq
from cadquery import exporters


@dataclass
class Params:
    wall_thickness: float = 2.4
    base_thickness: float = 2.8
    side_clearance: float = 2.0
    standoff_height: float = 3.0
    standoff_diameter: float = 6.0
    screw_hole_diameter: float = 2.8
    min_mount_hole_diameter: float = 2.0
    max_mount_hole_diameter: float = 6.5
    top_margin: float = 5.0
    connector_edge_threshold: float = 1.5
    connector_padding_xy: float = 1.2
    connector_padding_z: float = 0.8
    xy_tolerance: float = 0.06
    full_circle_tolerance: float = 0.03
    label_height: float = 0.35
    label_margin_ratio: float = 0.85
    label_height_ratio: float = 0.22
    label_char_width_factor: float = 0.62


@dataclass
class SideCutout:
    side: str
    center_xy: Tuple[float, float]
    z_bottom: float
    width_xy: float
    height_z: float


class ProgressBar:
    """Minimal terminal progress bar for long-running stages."""

    def __init__(self, total: int, title: str) -> None:
        self.total = max(1, int(total))
        self.current = 0
        self.title = title
        self._last_render_len = 0
        self._render("starting")

    def advance(self, message: str = "") -> None:
        self.current = min(self.total, self.current + 1)
        self._render(message)
        if self.current >= self.total:
            sys.stderr.write("\n")
            sys.stderr.flush()

    def _render(self, message: str) -> None:
        width = 28
        ratio = self.current / self.total
        filled = int(width * ratio)
        bar = "#" * filled + "-" * (width - filled)
        percent = int(ratio * 100.0)
        suffix = f" {message}" if message else ""
        line = f"[{self.title}] [{bar}] {percent:3d}% ({self.current}/{self.total}){suffix}"
        pad = " " * max(0, self._last_render_len - len(line))
        sys.stderr.write(f"\r{line}{pad}")
        self._last_render_len = len(line)
        sys.stderr.flush()


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


def write_standoff_template_pdf(
    output_pdf: Path,
    pcb_bbox,
    holes: List[Tuple[float, float, float]],
) -> None:
    """Write a 1:1 scale printable PDF showing PCB outline and standoff centers.

    The drawing is in mm units. For accurate physical size, print at 100% scale
    and disable any "fit to page" option in the print dialog.
    """
    try:
        import matplotlib.pyplot as plt
        import matplotlib.patches as patches
    except Exception as exc:
        raise RuntimeError(
            "matplotlib is required to create the standoff template PDF"
        ) from exc

    pcb_w = pcb_bbox.xmax - pcb_bbox.xmin
    pcb_h = pcb_bbox.ymax - pcb_bbox.ymin
    margin = 10.0

    page_w_mm = pcb_w + 2.0 * margin
    page_h_mm = pcb_h + 2.0 * margin

    fig = plt.figure(figsize=(page_w_mm / 25.4, page_h_mm / 25.4))
    ax = fig.add_axes([0.0, 0.0, 1.0, 1.0])
    ax.set_xlim(0.0, page_w_mm)
    ax.set_ylim(0.0, page_h_mm)
    ax.set_aspect("equal", adjustable="box")
    ax.axis("off")

    # PCB outline.
    pcb_rect = patches.Rectangle(
        (margin, margin),
        pcb_w,
        pcb_h,
        linewidth=0.8,
        edgecolor="black",
        facecolor="none",
    )
    ax.add_patch(pcb_rect)

    # Mounting-hole positions.
    for x, y, d in holes:
        hx = margin + (x - pcb_bbox.xmin)
        hy = margin + (y - pcb_bbox.ymin)

        hole = patches.Circle(
            (hx, hy),
            d / 2.0,
            linewidth=0.8,
            edgecolor="black",
            facecolor="none",
        )
        ax.add_patch(hole)

        # Center mark for easier punch/alignment checks.
        mark = 1.2
        ax.plot([hx - mark, hx + mark], [hy, hy], color="black", linewidth=0.5)
        ax.plot([hx, hx], [hy - mark, hy + mark], color="black", linewidth=0.5)

    fig.savefig(str(output_pdf), format="pdf", bbox_inches="tight", pad_inches=0.0)
    plt.close(fig)


def write_hole_debug_csv(output_csv: Path, rows: List[dict]) -> None:
    """Write hole-candidate debug rows for review and tuning."""
    fieldnames = [
        "face",
        "x_mm",
        "y_mm",
        "diameter_mm",
        "edge_ratio",
        "accepted",
        "reason",
    ]
    with output_csv.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def write_panel_local_holes_csv(output_csv: Path, rows: List[dict]) -> None:
    """Write local board hole coordinates and exclusion status for panel mode."""
    fieldnames = [
        "board",
        "step",
        "hole_index",
        "x_local_mm",
        "y_local_mm",
        "diameter_mm",
        "excluded",
        "reason",
    ]
    with output_csv.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def apply_standoff_exclusions(
    local_holes: List[Tuple[float, float, float]],
    exclusions: List,
    xy_tolerance: float,
) -> Tuple[List[Tuple[float, float, float]], Dict[int, str]]:
    """Exclude holes by local index and/or local XY coordinates."""
    excluded: Dict[int, str] = {}

    for rule in exclusions:
        if isinstance(rule, int):
            idx = rule
            if 0 <= idx < len(local_holes):
                excluded[idx] = "index"
            continue

        if isinstance(rule, list) and len(rule) >= 2:
            target_x = float(rule[0])
            target_y = float(rule[1])
            for i, (x, y, _d) in enumerate(local_holes):
                if abs(x - target_x) <= xy_tolerance and abs(y - target_y) <= xy_tolerance:
                    excluded[i] = "xy"
            continue

        if isinstance(rule, dict):
            if "index" in rule:
                idx = int(rule["index"])
                if 0 <= idx < len(local_holes):
                    excluded[idx] = "index"

            if "xy" in rule and isinstance(rule["xy"], (list, tuple)) and len(rule["xy"]) >= 2:
                target_x = float(rule["xy"][0])
                target_y = float(rule["xy"][1])
                for i, (x, y, _d) in enumerate(local_holes):
                    if abs(x - target_x) <= xy_tolerance and abs(y - target_y) <= xy_tolerance:
                        excluded[i] = "xy"

    kept = [hole for i, hole in enumerate(local_holes) if i not in excluded]
    return kept, excluded


def write_panel_standoff_template_pdf(
    output_pdf: Path,
    panel_width: float,
    panel_height: float,
    board_outlines: List[List[Tuple[float, float]]],
    board_centers: List[Dict[str, Any]],
    standoff_holes: List[Dict[str, Any]],
    board_labels: List[dict],
    grid_enabled: bool = True,
    grid_minor_mm: float = 5.0,
    grid_major_mm: float = 25.0,
) -> None:
    """Write a 1:1 printable PDF for a multi-board panel layout."""
    try:
        import matplotlib.pyplot as plt
        import matplotlib.patches as patches
    except Exception as exc:
        raise RuntimeError(
            "matplotlib is required to create the standoff template PDF"
        ) from exc

    fig = plt.figure(figsize=(panel_width / 25.4, panel_height / 25.4))
    ax = fig.add_axes([0.0, 0.0, 1.0, 1.0])
    ax.set_xlim(0.0, panel_width)
    ax.set_ylim(0.0, panel_height)
    ax.set_aspect("equal", adjustable="box")
    ax.axis("off")

    if grid_enabled:
        minor = max(1.0, float(grid_minor_mm))
        major = max(minor, float(grid_major_mm))

        x = 0.0
        while x <= panel_width + 1e-6:
            is_major = abs((x / major) - round(x / major)) < 1e-6
            ax.plot(
                [x, x],
                [0.0, panel_height],
                color="#888888" if is_major else "#C8C8C8",
                linewidth=0.35 if is_major else 0.2,
                zorder=0,
            )
            if is_major:
                ax.text(
                    x + 0.8,
                    0.8,
                    f"{int(round(x))}",
                    fontsize=4.5,
                    ha="left",
                    va="bottom",
                    color="#666666",
                )
            x += minor

        y = 0.0
        while y <= panel_height + 1e-6:
            is_major = abs((y / major) - round(y / major)) < 1e-6
            ax.plot(
                [0.0, panel_width],
                [y, y],
                color="#888888" if is_major else "#C8C8C8",
                linewidth=0.35 if is_major else 0.2,
                zorder=0,
            )
            if is_major:
                ax.text(
                    0.8,
                    y + 0.8,
                    f"{int(round(y))}",
                    fontsize=4.5,
                    ha="left",
                    va="bottom",
                    color="#666666",
                )
            y += minor

    border = patches.Rectangle(
        (0.0, 0.0),
        panel_width,
        panel_height,
        linewidth=0.8,
        edgecolor="black",
        facecolor="none",
    )
    ax.add_patch(border)

    for outline in board_outlines:
        poly = patches.Polygon(outline, closed=True, linewidth=0.7, edgecolor="black", facecolor="none")
        ax.add_patch(poly)

    for center in board_centers:
        cx = float(center["x"])
        cy = float(center["y"])
        ax.text(
            cx + 1.8,
            cy - 1.8,
            f"({cx:.1f}, {cy:.1f})",
            fontsize=5.0,
            ha="left",
            va="top",
            color="#333333",
        )

    for hole in standoff_holes:
        x = float(hole["x"])
        y = float(hole["y"])
        d = float(hole["d"])
        index_label = str(hole.get("index", ""))

        hole = patches.Circle((x, y), d / 2.0, linewidth=0.8, edgecolor="black", facecolor="none")
        ax.add_patch(hole)
        mark = 1.2
        ax.plot([x - mark, x + mark], [y, y], color="black", linewidth=0.5)
        ax.plot([x, x], [y - mark, y + mark], color="black", linewidth=0.5)

        if index_label:
            ax.text(
                x + 1.8,
                y + 1.8,
                index_label,
                fontsize=5.0,
                ha="left",
                va="bottom",
                color="black",
            )

    # Draw board labels so the printable template matches embossed panel text.
    for label_spec in board_labels:
        tx = float(label_spec["x"])
        ty = float(label_spec["y"])
        angle = float(label_spec["rotation"])
        lines = label_spec["lines"]
        font_size_mm = float(label_spec["font_size_mm"])
        line_pitch = font_size_mm * 1.28
        total_height = line_pitch * (len(lines) - 1)
        font_points = font_size_mm * 72.0 / 25.4

        for i, line in enumerate(lines):
            y_offset = total_height / 2.0 - i * line_pitch
            ox, oy = rotate_point(0.0, y_offset, angle)
            ax.text(
                tx + ox,
                ty + oy,
                line,
                fontsize=font_points,
                rotation=angle,
                rotation_mode="anchor",
                ha="center",
                va="center",
                color="black",
            )

    fig.savefig(str(output_pdf), format="pdf", bbox_inches="tight", pad_inches=0.0)
    plt.close(fig)


def largest_solid(shape: cq.Workplane):
    solids = shape.solids().vals()
    if not solids:
        raise RuntimeError("No solids found in STEP file")
    return max(solids, key=lambda s: s.Volume())


def planar_faces(solid):
    return [f for f in solid.Faces() if f.geomType() == "PLANE"]


def top_bottom_faces(solid):
    faces = planar_faces(solid)
    if not faces:
        raise RuntimeError("PCB solid has no planar faces")

    by_z = sorted(((f, f.Center().z) for f in faces), key=lambda item: item[1])
    return by_z[-1][0], by_z[0][0]


def extract_circles(
    face,
    params: Params,
    face_label: str,
    debug_rows: Optional[List[dict]] = None,
) -> List[Tuple[float, float, float]]:
    circles: List[Tuple[float, float, float]] = []
    for edge in face.Edges():
        if edge.geomType() != "CIRCLE":
            continue
        try:
            radius = edge.radius()
        except Exception:
            continue

        c = edge.Center()
        diameter = radius * 2.0

        # Ignore circle arcs (e.g., rounded PCB corners) and keep only full circles.
        full_len = 2.0 * math.pi * radius
        edge_len = edge.Length()
        if full_len <= 0.0:
            if debug_rows is not None:
                debug_rows.append(
                    {
                        "face": face_label,
                        "x_mm": f"{c.x:.4f}",
                        "y_mm": f"{c.y:.4f}",
                        "diameter_mm": f"{diameter:.4f}",
                        "edge_ratio": "0.0000",
                        "accepted": "no",
                        "reason": "invalid_circle_length",
                    }
                )
            continue
        ratio = edge_len / full_len
        if abs(ratio - 1.0) > params.full_circle_tolerance:
            if debug_rows is not None:
                debug_rows.append(
                    {
                        "face": face_label,
                        "x_mm": f"{c.x:.4f}",
                        "y_mm": f"{c.y:.4f}",
                        "diameter_mm": f"{diameter:.4f}",
                        "edge_ratio": f"{ratio:.4f}",
                        "accepted": "no",
                        "reason": "arc_not_full_circle",
                    }
                )
            continue

        circles.append((c.x, c.y, diameter))
        if debug_rows is not None:
            debug_rows.append(
                {
                    "face": face_label,
                    "x_mm": f"{c.x:.4f}",
                    "y_mm": f"{c.y:.4f}",
                    "diameter_mm": f"{diameter:.4f}",
                    "edge_ratio": f"{ratio:.4f}",
                    "accepted": "yes",
                    "reason": "full_circle_candidate",
                }
            )
    return circles


def find_through_holes(top_circles, bottom_circles, params: Params) -> List[Tuple[float, float, float]]:
    holes: List[Tuple[float, float, float]] = []

    for tx, ty, td in top_circles:
        if td < params.min_mount_hole_diameter or td > params.max_mount_hole_diameter:
            continue

        for bx, by, bd in bottom_circles:
            if abs(tx - bx) > params.xy_tolerance:
                continue
            if abs(ty - by) > params.xy_tolerance:
                continue
            if abs(td - bd) > 0.08:
                continue

            holes.append((tx, ty, td))
            break

    # Remove near-duplicates from repeated circular edges.
    deduped: List[Tuple[float, float, float]] = []
    for x, y, d in holes:
        if any(abs(x - px) <= params.xy_tolerance and abs(y - py) <= params.xy_tolerance for px, py, _ in deduped):
            continue
        deduped.append((x, y, d))

    return deduped


def detect_connector_cutouts(all_solids, pcb_solid, pcb_bbox, params: Params) -> List[SideCutout]:
    cutouts: List[SideCutout] = []

    pcb_cx = (pcb_bbox.xmin + pcb_bbox.xmax) / 2.0
    pcb_cy = (pcb_bbox.ymin + pcb_bbox.ymax) / 2.0
    pcb_bottom = pcb_bbox.zmin

    for solid in all_solids:
        if solid.wrapped.IsSame(pcb_solid.wrapped):
            continue

        bbox = solid.BoundingBox()
        x_len = bbox.xmax - bbox.xmin
        y_len = bbox.ymax - bbox.ymin
        z_len = bbox.zmax - bbox.zmin

        if max(x_len, y_len, z_len) < 1.0:
            continue

        d_left = abs(bbox.xmin - pcb_bbox.xmin)
        d_right = abs(pcb_bbox.xmax - bbox.xmax)
        d_bottom = abs(bbox.ymin - pcb_bbox.ymin)
        d_top = abs(pcb_bbox.ymax - bbox.ymax)

        min_dist = min(d_left, d_right, d_bottom, d_top)
        if min_dist > params.connector_edge_threshold:
            continue

        side = "x-"
        if min_dist == d_right:
            side = "x+"
        elif min_dist == d_bottom:
            side = "y-"
        elif min_dist == d_top:
            side = "y+"

        cx = (bbox.xmin + bbox.xmax) / 2.0
        cy = (bbox.ymin + bbox.ymax) / 2.0
        z0 = bbox.zmin - pcb_bottom

        z_bottom = z0 - params.connector_padding_z
        height_z = z_len + 2.0 * params.connector_padding_z

        if side in ("x-", "x+"):
            width_xy = y_len + 2.0 * params.connector_padding_xy
        else:
            width_xy = x_len + 2.0 * params.connector_padding_xy

        cutouts.append(
            SideCutout(
                side=side,
                center_xy=(cx - pcb_cx, cy - pcb_cy),
                z_bottom=z_bottom,
                width_xy=width_xy,
                height_z=height_z,
            )
        )

    return cutouts


def _require_yaml_module():
    try:
        import yaml
    except Exception as exc:
        raise RuntimeError(
            "PyYAML is required for layout and intermediate YAML support. Install with: pip install pyyaml"
        ) from exc
    return yaml


def write_yaml_file(path: Path, data: Dict[str, Any]) -> None:
    yaml = _require_yaml_module()
    with path.open("w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, sort_keys=False)


def load_yaml_file(path: Path) -> Dict[str, Any]:
    yaml = _require_yaml_module()
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        raise RuntimeError(f"YAML root must be a mapping: {path}")
    return data


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


def load_layout_yaml(layout_yaml: Path) -> Dict:
    data = load_yaml_file(layout_yaml)

    if "boards" not in data or not isinstance(data["boards"], list) or not data["boards"]:
        raise RuntimeError("Layout YAML must contain a non-empty 'boards' list")

    return data


def analyze_board(step_path: Path, params: Params) -> Dict:
    shape = cq.importers.importStep(str(step_path))
    all_solids = shape.solids().vals()
    if not all_solids:
        raise RuntimeError(f"No solids found in STEP file: {step_path}")

    pcb = largest_solid(shape)
    pcb_bbox = pcb.BoundingBox()
    pcb_w = pcb_bbox.xmax - pcb_bbox.xmin
    pcb_h = pcb_bbox.ymax - pcb_bbox.ymin
    pcb_cx = (pcb_bbox.xmin + pcb_bbox.xmax) / 2.0
    pcb_cy = (pcb_bbox.ymin + pcb_bbox.ymax) / 2.0

    top_face, bottom_face = top_bottom_faces(pcb)
    top_circles = extract_circles(top_face, params, "top")
    bottom_circles = extract_circles(bottom_face, params, "bottom")
    holes = find_through_holes(top_circles, bottom_circles, params)
    cutouts = detect_connector_cutouts(all_solids, pcb, pcb_bbox, params)

    local_holes = [(x - pcb_cx, y - pcb_cy, d) for x, y, d in holes]
    local_cutouts = [
        {
            "side": c.side,
            "x_local_mm": c.center_xy[0],
            "y_local_mm": c.center_xy[1],
            "z_bottom_mm": c.z_bottom,
            "width_mm": c.width_xy,
            "height_mm": c.height_z,
        }
        for c in cutouts
    ]

    return {
        "pcb_w": pcb_w,
        "pcb_h": pcb_h,
        "local_holes": local_holes,
        "local_cutouts": local_cutouts,
    }


def parse_template_size(board: Dict[str, Any], idx: int) -> Tuple[float, float]:
    size_mm = board.get("size_mm")
    if not isinstance(size_mm, (list, tuple)) or len(size_mm) < 2:
        raise RuntimeError(f"boards[{idx}] kind=template requires size_mm: [width, height]")

    try:
        w = float(size_mm[0])
        h = float(size_mm[1])
    except Exception as exc:
        raise RuntimeError(f"boards[{idx}].size_mm values must be numeric") from exc

    if w <= 0.0 or h <= 0.0:
        raise RuntimeError(f"boards[{idx}].size_mm values must be > 0")
    return w, h


def parse_template_standoffs(
    board: Dict[str, Any],
    idx: int,
    default_hole_diameter: float,
) -> List[Tuple[float, float, float]]:
    standoffs = board.get("standoffs", [])
    if standoffs is None:
        standoffs = []
    if not isinstance(standoffs, list):
        raise RuntimeError(f"boards[{idx}].standoffs must be a list for kind=template")

    holes: List[Tuple[float, float, float]] = []
    for item_index, item in enumerate(standoffs):
        if not isinstance(item, dict):
            raise RuntimeError(f"boards[{idx}].standoffs[{item_index}] must be a mapping")
        xy = item.get("xy")
        if not isinstance(xy, (list, tuple)) or len(xy) < 2:
            raise RuntimeError(f"boards[{idx}].standoffs[{item_index}] requires xy: [x, y]")

        try:
            x = float(xy[0])
            y = float(xy[1])
            d = float(item.get("hole_diameter", item.get("hole_diameter_mm", default_hole_diameter)))
        except Exception as exc:
            raise RuntimeError(
                f"boards[{idx}].standoffs[{item_index}] contains non-numeric values"
            ) from exc

        if d <= 0.0:
            raise RuntimeError(f"boards[{idx}].standoffs[{item_index}] hole diameter must be > 0")
        holes.append((x, y, d))

    return sorted(holes, key=lambda hole: (hole[1], hole[0]))


def parse_template_cutouts(board: Dict[str, Any], idx: int) -> List[Dict[str, float]]:
    cutouts = board.get("wall_cutouts", [])
    if cutouts is None:
        cutouts = []
    if not isinstance(cutouts, list):
        raise RuntimeError(f"boards[{idx}].wall_cutouts must be a list for kind=template")

    parsed: List[Dict[str, float]] = []
    for item_index, item in enumerate(cutouts):
        if not isinstance(item, dict):
            raise RuntimeError(f"boards[{idx}].wall_cutouts[{item_index}] must be a mapping")

        side = str(item.get("side", "")).strip().lower()
        if side not in ("x-", "x+", "y-", "y+"):
            raise RuntimeError(
                f"boards[{idx}].wall_cutouts[{item_index}].side must be one of x-, x+, y-, y+"
            )

        xy = item.get("xy")
        if not isinstance(xy, (list, tuple)) or len(xy) < 2:
            raise RuntimeError(f"boards[{idx}].wall_cutouts[{item_index}] requires xy: [x, y]")

        try:
            x_local = float(xy[0])
            y_local = float(xy[1])
            z_bottom = float(item.get("z_bottom_mm", 0.0))
            width = float(item.get("width_mm", item.get("width", 0.0)))
            height = float(item.get("height_mm", item.get("height", 0.0)))
        except Exception as exc:
            raise RuntimeError(
                f"boards[{idx}].wall_cutouts[{item_index}] contains non-numeric values"
            ) from exc

        if width <= 0.0 or height <= 0.0:
            raise RuntimeError(
                f"boards[{idx}].wall_cutouts[{item_index}] width/height must be > 0"
            )

        parsed.append(
            {
                "side": side,
                "x_local_mm": x_local,
                "y_local_mm": y_local,
                "z_bottom_mm": z_bottom,
                "width_mm": width,
                "height_mm": height,
            }
        )

    return parsed


def analyze_layout_to_intermediate(
    layout_yaml: Path,
    intermediate_yaml: Path,
    output_local_holes_csv: Path,
    params: Params,
) -> None:
    data = load_layout_yaml(layout_yaml)

    panel_cfg = data.get("panel", {})
    if not isinstance(panel_cfg, dict):
        raise RuntimeError("'panel' must be a mapping when provided")

    panel_margin = float(panel_cfg.get("margin", 8.0))
    base_thickness = float(panel_cfg.get("base_thickness", params.base_thickness))
    wall_height = float(panel_cfg.get("wall_height", 0.0))
    wall_thickness = float(panel_cfg.get("wall_thickness", params.wall_thickness))
    standoff_height_default = float(panel_cfg.get("standoff_height", params.standoff_height))
    standoff_diameter = float(panel_cfg.get("standoff_diameter", params.standoff_diameter))
    screw_hole_diameter = float(panel_cfg.get("screw_hole_diameter", params.screw_hole_diameter))
    label_height = float(panel_cfg.get("label_height", params.label_height))
    auto_gap_x = float(panel_cfg.get("auto_gap_x", panel_margin))
    auto_gap_y = float(panel_cfg.get("auto_gap_y", panel_margin))
    auto_origin_x = float(panel_cfg.get("auto_origin_x", 0.0))
    auto_origin_y = float(panel_cfg.get("auto_origin_y", 0.0))
    pdf_grid_enabled = bool(panel_cfg.get("pdf_grid_enabled", True))
    pdf_grid_minor_mm = float(panel_cfg.get("pdf_grid_minor_mm", 5.0))
    pdf_grid_major_mm = float(panel_cfg.get("pdf_grid_major_mm", 25.0))
    _max_size_raw = panel_cfg.get("max_panel_size_mm", [250.0, 250.0])
    if not (isinstance(_max_size_raw, (list, tuple)) and len(_max_size_raw) == 2):
        raise RuntimeError("panel.max_panel_size_mm must be a list of [width, height]")
    max_panel_w = float(_max_size_raw[0])
    max_panel_h = float(_max_size_raw[1])
    max_usable_w = max_panel_w - 2.0 * panel_margin

    if wall_height < 0.0:
        raise RuntimeError("panel.wall_height must be >= 0")
    if pdf_grid_minor_mm <= 0.0:
        raise RuntimeError("panel.pdf_grid_minor_mm must be > 0")
    if pdf_grid_major_mm <= 0.0:
        raise RuntimeError("panel.pdf_grid_major_mm must be > 0")

    board_inputs = []
    placements = []
    all_outline_pts = []
    local_hole_rows: List[dict] = []
    analyze_bar = ProgressBar(len(data["boards"]), "analyze")

    for idx, board in enumerate(data["boards"]):
        if not isinstance(board, dict):
            raise RuntimeError(f"boards[{idx}] must be a mapping")

        board_kind = str(board.get("kind", "step")).strip().lower()
        if board_kind not in ("step", "template"):
            raise RuntimeError(f"boards[{idx}].kind must be 'step' or 'template'")

        name = str(board.get("name", f"board_{idx + 1}"))
        # Source x/y are intentionally ignored; placement comes from auto-layout
        # and can be edited in the intermediate YAML.
        rotation = float(board.get("rotation", 0.0))
        board_standoff_height = float(board.get("standoff_height", standoff_height_default))
        board_standoff_diameter = float(board.get("standoff_diameter", standoff_diameter))
        board_screw_hole_diameter = float(board.get("screw_hole_diameter", screw_hole_diameter))
        label_rotation = float(board.get("label_rotation", 0.0))
        label_enabled = bool(board.get("label_enabled", True))

        step_name = ""
        if board_kind == "step":
            step_val = board.get("step")
            if not step_val:
                raise RuntimeError(f"boards[{idx}] kind=step is missing required field: step")

            step_path = (layout_yaml.parent / str(step_val)).resolve()
            if not step_path.exists():
                raise RuntimeError(f"boards[{idx}] STEP not found: {step_path}")

            step_name = step_path.name
            label = str(board.get("label", step_path.stem))

            analyzed = analyze_board(step_path, params)
            w = analyzed["pcb_w"]
            h = analyzed["pcb_h"]
            local_holes_all = sorted(analyzed["local_holes"], key=lambda hole: (hole[1], hole[0]))
            local_cutouts = analyzed["local_cutouts"] if wall_height > 0.0 else []
        else:
            label = str(board.get("label", name))
            w, h = parse_template_size(board, idx)
            local_holes_all = parse_template_standoffs(board, idx, board_screw_hole_diameter)
            local_cutouts = parse_template_cutouts(board, idx) if wall_height > 0.0 else []

        if "exclude_standoffs" in board:
            print(
                f"Note: boards[{idx}].exclude_standoffs is ignored in source layout; "
                "exclude standoffs in intermediate YAML via features.standoffs[].enabled"
            )

        excluded_map: Dict[int, str] = {}

        for hole_index, (hx, hy, hd) in enumerate(local_holes_all):
            reason = excluded_map.get(hole_index, "")
            local_hole_rows.append(
                {
                    "board": name,
                    "step": step_name,
                    "hole_index": hole_index,
                    "x_local_mm": f"{hx:.4f}",
                    "y_local_mm": f"{hy:.4f}",
                    "diameter_mm": f"{hd:.4f}",
                    "excluded": "yes" if hole_index in excluded_map else "no",
                    "reason": reason,
                }
            )

        board_inputs.append(
            {
                "name": name,
                "step": step_name,
                "kind": board_kind,
                "label": label,
                "label_enabled": label_enabled,
                "label_rotation": label_rotation,
                "board_w": w,
                "board_h": h,
                "rotation": rotation,
                "standoff_height": board_standoff_height,
                "standoff_diameter": board_standoff_diameter,
                "screw_hole_diameter": board_screw_hole_diameter,
                "detected_holes": len(local_holes_all),
                "excluded_holes": 0,
                "active_holes": len(local_holes_all),
                "local_holes_all": local_holes_all,
                "local_cutouts": local_cutouts,
                "excluded_map": excluded_map,
            }
        )
        analyze_bar.advance(f"board {idx + 1}/{len(data['boards'])}: {name}")

    # Auto-place boards left-to-right, wrapping to new rows when max_panel_w is exceeded.
    auto_cursor_x = auto_origin_x
    auto_cursor_y = auto_origin_y
    row_max_h = 0.0
    place_bar = ProgressBar(len(board_inputs), "place")
    for board in board_inputs:
        angle = math.radians(float(board["rotation"]))
        footprint_w = abs(float(board["board_w"]) * math.cos(angle)) + abs(float(board["board_h"]) * math.sin(angle))
        footprint_h = abs(float(board["board_h"]) * math.cos(angle)) + abs(float(board["board_w"]) * math.sin(angle))

        # Wrap to a new row if this board would exceed the usable panel width.
        if auto_cursor_x > auto_origin_x and auto_cursor_x + footprint_w > auto_origin_x + max_usable_w:
            auto_cursor_y += row_max_h + auto_gap_y
            auto_cursor_x = auto_origin_x
            row_max_h = 0.0

        x = auto_cursor_x + footprint_w / 2.0
        y = auto_cursor_y

        auto_cursor_x = float(x) + footprint_w / 2.0 + auto_gap_x
        row_max_h = max(row_max_h, footprint_h)

        corners_local = [
            (-float(board["board_w"]) / 2.0, -float(board["board_h"]) / 2.0),
            (float(board["board_w"]) / 2.0, -float(board["board_h"]) / 2.0),
            (float(board["board_w"]) / 2.0, float(board["board_h"]) / 2.0),
            (-float(board["board_w"]) / 2.0, float(board["board_h"]) / 2.0),
        ]
        corners_world = []
        for cx, cy in corners_local:
            rx, ry = rotate_point(cx, cy, float(board["rotation"]))
            wx = float(x) + rx
            wy = float(y) + ry
            corners_world.append((wx, wy))
            all_outline_pts.append((wx, wy))

        standoff_features = []
        for hole_index, (hx, hy, d) in enumerate(board["local_holes_all"]):
            standoff_features.append(
                {
                    "id": f"{board['name']}:standoff:{hole_index}",
                    "hole_index": hole_index,
                    "enabled": True,
                    "exclude_reason": "",
                    "x_local_mm": hx,
                    "y_local_mm": hy,
                    "hole_diameter_mm": d,
                    "standoff_diameter_mm": board["standoff_diameter"],
                    "screw_hole_diameter_mm": board["screw_hole_diameter"],
                    "standoff_height_mm": board["standoff_height"],
                }
            )

        cutout_features = []
        for cutout_index, cut in enumerate(board["local_cutouts"]):
            cutout_features.append(
                {
                    "id": f"{board['name']}:cutout:{cutout_index}",
                    "enabled": True,
                    "side": str(cut["side"]),
                    "x_local_mm": float(cut["x_local_mm"]),
                    "y_local_mm": float(cut["y_local_mm"]),
                    # z_bottom is recorded relative to base top, including this board's standoff height.
                    "z_bottom_mm": float(board["standoff_height"]) + float(cut["z_bottom_mm"]),
                    "width_mm": float(cut["width_mm"]),
                    "height_mm": float(cut["height_mm"]),
                }
            )

        label_lines, label_font_size = wrap_label_to_fit(
            str(board["label"]),
            float(board["board_w"]),
            float(board["board_h"]),
            params,
        )

        placements.append(
            {
                "name": board["name"],
                "step": board["step"],
                "kind": board["kind"],
                "label": board["label"],
                "label_enabled": board["label_enabled"],
                "label_rotation": board["label_rotation"],
                "label_lines": label_lines,
                "label_font_size_mm": label_font_size,
                "board_w": board["board_w"],
                "board_h": board["board_h"],
                "x": float(x),
                "y": float(y),
                "rotation": board["rotation"],
                "standoff_height": board["standoff_height"],
                "standoff_diameter": board["standoff_diameter"],
                "screw_hole_diameter": board["screw_hole_diameter"],
                "detected_holes": board["detected_holes"],
                "excluded_holes": board["excluded_holes"],
                "active_holes": board["active_holes"],
                "corners_world": corners_world,
                "standoffs": standoff_features,
                "wall_cutouts": cutout_features,
            }
        )
        place_bar.advance(f"{board['name']}")

    if not all_outline_pts:
        raise RuntimeError("No board geometry available from layout")

    min_x = min(p[0] for p in all_outline_pts)
    max_x = max(p[0] for p in all_outline_pts)
    min_y = min(p[1] for p in all_outline_pts)
    max_y = max(p[1] for p in all_outline_pts)

    panel_w = (max_x - min_x) + 2.0 * panel_margin
    panel_h = (max_y - min_y) + 2.0 * panel_margin

    def to_panel(px: float, py: float) -> Tuple[float, float]:
        return px - min_x + panel_margin, py - min_y + panel_margin

    boards_spec = []
    total_standoffs = 0
    total_cutouts = 0
    emit_bar = ProgressBar(len(placements), "emit")

    for placement in placements:
        standoff_specs = [dict(standoff) for standoff in placement["standoffs"]]
        cutout_specs = [dict(cutout) for cutout in placement["wall_cutouts"]]

        label_x, label_y = to_panel(placement["x"], placement["y"])

        boards_spec.append(
            {
                "id": placement["name"],
                "name": placement["name"],
                "step": placement.get("step", ""),
                "kind": placement.get("kind", "step"),
                # Board center in panel coordinates; edit these to reposition the board.
                "x_mm": label_x,
                "y_mm": label_y,
                "rotation_deg": placement["rotation"],
                "board_w_mm": placement["board_w"],
                "board_h_mm": placement["board_h"],
                "features": {
                    "standoffs": standoff_specs,
                    "wall_cutouts": cutout_specs,
                    "label": {
                        "id": f"{placement['name']}:label",
                        "enabled": placement["label_enabled"],
                        "x_local_mm": 0.0,
                        "y_local_mm": 0.0,
                        "rotation_deg": placement["label_rotation"],
                        "lines": placement["label_lines"],
                        "font_size_mm": placement["label_font_size_mm"],
                    },
                },
            }
        )
        total_standoffs += sum(1 for s in standoff_specs if bool(s.get("enabled", True)))
        total_cutouts += sum(1 for c in cutout_specs if bool(c.get("enabled", True)))
        emit_bar.advance(f"{placement['name']}")

    intermediate = {
        "schema_version": 1,
        "source_layout": str(layout_yaml),
        "panel": {
            "margin_mm": panel_margin,
            "base_thickness_mm": base_thickness,
            "wall_height_mm": wall_height,
            "wall_thickness_mm": wall_thickness,
            "label_height_mm": label_height,
            "pdf_grid_enabled": pdf_grid_enabled,
            "pdf_grid_minor_mm": pdf_grid_minor_mm,
            "pdf_grid_major_mm": pdf_grid_major_mm,
        },
        "panel_size_mm": {
            "width": panel_w,
            "height": panel_h,
        },
        "boards": boards_spec,
    }

    write_yaml_file(intermediate_yaml, intermediate)
    write_panel_local_holes_csv(output_local_holes_csv, local_hole_rows)

    print(f"Input layout YAML: {layout_yaml}")
    print(f"Output intermediate YAML: {intermediate_yaml}")
    print(f"Output local holes CSV: {output_local_holes_csv}")
    print(f"Boards placed: {len(placements)}")
    print(f"Enabled standoffs: {total_standoffs}")
    print(f"Enabled wall cutouts: {total_cutouts}")
    print(f"Panel size (mm): {panel_w:.2f} x {panel_h:.2f}")


def build_from_intermediate(
    intermediate_yaml: Path,
    output_stl: Path,
    output_pdf: Path,
    params: Params,
) -> None:
    data = load_yaml_file(intermediate_yaml)
    if int(data.get("schema_version", 0)) != 1:
        raise RuntimeError("Unsupported intermediate schema_version (expected 1)")

    panel_cfg = data.get("panel", {})
    if not isinstance(panel_cfg, dict):
        raise RuntimeError("Intermediate panel section must be a mapping")

    panel_size = data.get("panel_size_mm", {})
    if not isinstance(panel_size, dict):
        raise RuntimeError("Intermediate panel_size_mm section must be a mapping")

    panel_w = float(panel_size.get("width", 0.0))
    panel_h = float(panel_size.get("height", 0.0))
    if panel_w <= 0.0 or panel_h <= 0.0:
        raise RuntimeError("Intermediate panel size must have positive width/height")

    base_thickness = float(panel_cfg.get("base_thickness_mm", params.base_thickness))
    wall_height = float(panel_cfg.get("wall_height_mm", 0.0))
    wall_thickness = float(panel_cfg.get("wall_thickness_mm", params.wall_thickness))
    label_height = float(panel_cfg.get("label_height_mm", params.label_height))
    pdf_grid_enabled = bool(panel_cfg.get("pdf_grid_enabled", True))
    pdf_grid_minor_mm = float(panel_cfg.get("pdf_grid_minor_mm", 5.0))
    pdf_grid_major_mm = float(panel_cfg.get("pdf_grid_major_mm", 25.0))

    boards = data.get("boards", [])
    if not isinstance(boards, list) or not boards:
        raise RuntimeError("Intermediate YAML must contain a non-empty boards list")

    if wall_height > 0.0:
        outer_w = panel_w + 2.0 * wall_thickness
        outer_h = panel_h + 2.0 * wall_thickness
        model = cq.Workplane("XY").box(
            outer_w,
            outer_h,
            base_thickness + wall_height,
            centered=(False, False, False),
        )
        cavity = (
            cq.Workplane("XY")
            .transformed(offset=(wall_thickness, wall_thickness, base_thickness))
            .box(panel_w, panel_h, wall_height + 0.01, centered=(False, False, False))
        )
        model = model.cut(cavity)
        panel_x_offset = wall_thickness
        panel_y_offset = wall_thickness
    else:
        model = cq.Workplane("XY").box(
            panel_w,
            panel_h,
            base_thickness,
            centered=(False, False, False),
        )
        outer_w = panel_w
        outer_h = panel_h
        panel_x_offset = 0.0
        panel_y_offset = 0.0

    pdf_outlines: List[List[Tuple[float, float]]] = []
    pdf_centers: List[Dict[str, Any]] = []
    pdf_holes: List[Dict[str, Any]] = []
    pdf_labels: List[dict] = []

    enabled_standoffs = 0
    enabled_cutouts = 0
    build_bar = ProgressBar(len(boards), "build")

    for board in boards:
        board_name = str(board.get("name", "board"))
        if "x_mm" not in board or "y_mm" not in board:
            raise RuntimeError(
                "Intermediate YAML boards must include x_mm and y_mm; "
                "run analyze stage or add them manually before build"
            )
        try:
            float(board["x_mm"])
            float(board["y_mm"])
        except Exception as exc:
            raise RuntimeError(
                "Intermediate YAML board x_mm/y_mm must be numeric"
            ) from exc

        features = board.get("features", {})
        if not isinstance(features, dict):
            build_bar.advance(board_name)
            continue

        label = features.get("label", {})
        standoffs = features.get("standoffs", [])
        target_center_x = float(board["x_mm"])
        target_center_y = float(board["y_mm"])

        # Board outline for PDF is derived from center/size/rotation.
        board_w_mm = float(board.get("board_w_mm", 0.0))
        board_h_mm = float(board.get("board_h_mm", 0.0))
        board_rot = float(board.get("rotation_deg", 0.0))
        if board_w_mm > 0.0 and board_h_mm > 0.0:
            corners_local = [
                (-board_w_mm / 2.0, -board_h_mm / 2.0),
                (board_w_mm / 2.0, -board_h_mm / 2.0),
                (board_w_mm / 2.0, board_h_mm / 2.0),
                (-board_w_mm / 2.0, board_h_mm / 2.0),
            ]
            outline_pts: List[Tuple[float, float]] = []
            for cx, cy in corners_local:
                rx, ry = rotate_point(cx, cy, board_rot)
                outline_pts.append((target_center_x + rx, target_center_y + ry))
            pdf_outlines.append(outline_pts)

        pdf_centers.append(
            {
                "name": board_name,
                "x": target_center_x,
                "y": target_center_y,
            }
        )

        if isinstance(standoffs, list):
            for standoff in standoffs:
                if not isinstance(standoff, dict) or not bool(standoff.get("enabled", True)):
                    continue

                local_x = float(standoff.get("x_local_mm", 0.0))
                local_y = float(standoff.get("y_local_mm", 0.0))
                rx, ry = rotate_point(local_x, local_y, board_rot)
                standoff_x = target_center_x + rx
                standoff_y = target_center_y + ry
                sx = panel_x_offset + standoff_x
                sy = panel_y_offset + standoff_y
                standoff_d = float(standoff["standoff_diameter_mm"])
                screw_d = float(standoff["screw_hole_diameter_mm"])
                standoff_h = float(standoff["standoff_height_mm"])

                standoff_solid = (
                    cq.Workplane("XY")
                    .workplane(offset=base_thickness)
                    .center(sx, sy)
                    .circle(standoff_d / 2.0)
                    .extrude(standoff_h)
                )
                model = model.union(standoff_solid)

                screw_hole = (
                    cq.Workplane("XY")
                    .workplane(offset=base_thickness - 0.01)
                    .center(sx, sy)
                    .circle(screw_d / 2.0)
                    .extrude(standoff_h + 0.5)
                )
                model = model.cut(screw_hole)

                pdf_holes.append(
                    {
                        "x": standoff_x,
                        "y": standoff_y,
                        "d": float(standoff.get("hole_diameter_mm", screw_d)),
                        "index": int(standoff.get("hole_index", -1)),
                    }
                )
                enabled_standoffs += 1

        if isinstance(label, dict) and bool(label.get("enabled", True)):
            label_local_x = float(label.get("x_local_mm", 0.0))
            label_local_y = float(label.get("y_local_mm", 0.0))
            lrx, lry = rotate_point(label_local_x, label_local_y, board_rot)
            label_x = target_center_x + lrx
            label_y = target_center_y + lry
            tx = panel_x_offset + label_x
            ty = panel_y_offset + label_y
            angle = board_rot + float(label.get("rotation_deg", 0.0))
            lines = label.get("lines", [])
            if not isinstance(lines, list):
                lines = [str(lines)]
            lines = [str(line) for line in lines if str(line).strip()]
            if lines:
                font_size = float(label.get("font_size_mm", 2.0))
                line_pitch = font_size * 1.28
                total_height = line_pitch * (len(lines) - 1)

                for i, line in enumerate(lines):
                    y_offset = total_height / 2.0 - i * line_pitch
                    text_wp = (
                        cq.Workplane("XY")
                        .transformed(offset=(tx, ty, base_thickness), rotate=(0, 0, angle))
                        .center(0.0, y_offset)
                        .text(
                            line,
                            fontsize=font_size,
                            distance=label_height,
                            combine=False,
                            halign="center",
                            valign="center",
                        )
                    )
                    model = model.union(text_wp)

                pdf_labels.append(
                    {
                        "x": label_x,
                        "y": label_y,
                        "rotation": angle,
                        "lines": lines,
                        "font_size_mm": font_size,
                    }
                )

        if wall_height > 0.0:
            cutouts = features.get("wall_cutouts", [])
            if isinstance(cutouts, list):
                for cutout in cutouts:
                    if not isinstance(cutout, dict) or not bool(cutout.get("enabled", True)):
                        continue

                    side = rotate_side(str(cutout.get("side", "x-")), board_rot)
                    cut_local_x = float(cutout.get("x_local_mm", 0.0))
                    cut_local_y = float(cutout.get("y_local_mm", 0.0))
                    crx, cry = rotate_point(cut_local_x, cut_local_y, board_rot)
                    local_x = panel_x_offset + target_center_x + crx
                    local_y = panel_y_offset + target_center_y + cry
                    z_bottom = max(base_thickness + 0.2, base_thickness + float(cutout.get("z_bottom_mm", 0.0)))
                    width_xy = float(cutout.get("width_mm", 0.0))
                    height_z = float(cutout.get("height_mm", 0.0))
                    if width_xy <= 0.0 or height_z <= 0.0:
                        continue

                    if side == "x-":
                        cutter = (
                            cq.Workplane("XY")
                            .transformed(offset=(wall_thickness / 2.0, local_y, z_bottom))
                            .box(wall_thickness + 0.8, width_xy, height_z, centered=(True, True, False))
                        )
                    elif side == "x+":
                        cutter = (
                            cq.Workplane("XY")
                            .transformed(offset=(outer_w - wall_thickness / 2.0, local_y, z_bottom))
                            .box(wall_thickness + 0.8, width_xy, height_z, centered=(True, True, False))
                        )
                    elif side == "y-":
                        cutter = (
                            cq.Workplane("XY")
                            .transformed(offset=(local_x, wall_thickness / 2.0, z_bottom))
                            .box(width_xy, wall_thickness + 0.8, height_z, centered=(True, True, False))
                        )
                    elif side == "y+":
                        cutter = (
                            cq.Workplane("XY")
                            .transformed(offset=(local_x, outer_h - wall_thickness / 2.0, z_bottom))
                            .box(width_xy, wall_thickness + 0.8, height_z, centered=(True, True, False))
                        )
                    else:
                        raise RuntimeError(f"Invalid cutout side in intermediate YAML: {side}")

                    model = model.cut(cutter)
                    enabled_cutouts += 1

        build_bar.advance(board_name)

    # Center model in XY so slicers with different bed-origin assumptions place it consistently.
    model = model.translate((-outer_w / 2.0, -outer_h / 2.0, 0.0))
    exporters.export(model, str(output_stl))
    write_panel_standoff_template_pdf(
        output_pdf,
        panel_w,
        panel_h,
        pdf_outlines,
        pdf_centers,
        pdf_holes,
        pdf_labels,
        grid_enabled=pdf_grid_enabled,
        grid_minor_mm=pdf_grid_minor_mm,
        grid_major_mm=pdf_grid_major_mm,
    )

    print(f"Input intermediate YAML: {intermediate_yaml}")
    print(f"Output enclosure STL: {output_stl}")
    print(f"Output standoff template PDF: {output_pdf}")
    print(f"Panel size (mm): {panel_w:.2f} x {panel_h:.2f}")
    print(f"Enabled standoffs: {enabled_standoffs}")
    print(f"Enabled wall cutouts: {enabled_cutouts}")
    if wall_height <= 0.0:
        print("Mode: panel/base only (wall_height_mm <= 0)")
    else:
        print(f"Wall height (mm): {wall_height:.2f}")


def build_panel(
    layout_yaml: Path,
    output_stl: Path,
    output_pdf: Path,
    output_local_holes_csv: Path,
    params: Params,
) -> None:
    intermediate_yaml = layout_yaml.with_name(f"{layout_yaml.stem}_intermediate.yaml")
    analyze_layout_to_intermediate(layout_yaml, intermediate_yaml, output_local_holes_csv, params)
    build_from_intermediate(intermediate_yaml, output_stl, output_pdf, params)
    print("Compatibility mode: --layout-yaml now uses analyze + build via intermediate YAML.")


def build_enclosure(
    step_path: Path,
    output_stl: Path,
    output_pdf: Path,
    params: Params,
    debug_holes_csv: Optional[Path] = None,
    base_only: bool = False,
) -> None:
    shape = cq.importers.importStep(str(step_path))
    all_solids = shape.solids().vals()
    if not all_solids:
        raise RuntimeError("No solids found in STEP file")

    pcb = largest_solid(shape)
    pcb_bbox = pcb.BoundingBox()

    pcb_w = pcb_bbox.xmax - pcb_bbox.xmin
    pcb_h = pcb_bbox.ymax - pcb_bbox.ymin
    pcb_bottom = pcb_bbox.zmin

    model_bbox = shape.val().BoundingBox()
    tallest_above_pcb_bottom = model_bbox.zmax - pcb_bottom

    top_face, bottom_face = top_bottom_faces(pcb)
    hole_debug_rows: Optional[List[dict]] = [] if debug_holes_csv else None
    top_circles = extract_circles(top_face, params, "top", hole_debug_rows)
    bottom_circles = extract_circles(bottom_face, params, "bottom", hole_debug_rows)
    holes = find_through_holes(top_circles, bottom_circles, params)

    if debug_holes_csv and hole_debug_rows is not None:
        write_hole_debug_csv(debug_holes_csv, hole_debug_rows)

    write_standoff_template_pdf(output_pdf, pcb_bbox, holes)

    cutouts = [] if base_only else detect_connector_cutouts(all_solids, pcb, pcb_bbox, params)

    inner_w = pcb_w + 2.0 * params.side_clearance
    inner_h = pcb_h + 2.0 * params.side_clearance
    if base_only:
        outer_w = inner_w
        outer_h = inner_h
    else:
        outer_w = inner_w + 2.0 * params.wall_thickness
        outer_h = inner_h + 2.0 * params.wall_thickness

    wall_height = params.standoff_height + tallest_above_pcb_bottom + params.top_margin
    total_height = params.base_thickness if base_only else params.base_thickness + wall_height

    enclosure = cq.Workplane("XY").box(
        outer_w,
        outer_h,
        total_height,
        centered=(False, False, False),
    )

    if not base_only:
        cavity = (
            cq.Workplane("XY")
            .transformed(offset=(params.wall_thickness, params.wall_thickness, params.base_thickness))
            .box(inner_w, inner_h, wall_height + 0.01, centered=(False, False, False))
        )
        enclosure = enclosure.cut(cavity)

    pcb_cx = (pcb_bbox.xmin + pcb_bbox.xmax) / 2.0
    pcb_cy = (pcb_bbox.ymin + pcb_bbox.ymax) / 2.0
    if base_only:
        inner_cx = inner_w / 2.0
        inner_cy = inner_h / 2.0
    else:
        inner_cx = params.wall_thickness + inner_w / 2.0
        inner_cy = params.wall_thickness + inner_h / 2.0

    standoff_points = []
    for x, y, _d in holes:
        lx = inner_cx + (x - pcb_cx)
        ly = inner_cy + (y - pcb_cy)
        standoff_points.append((lx, ly))

    if standoff_points:
        standoffs = (
            cq.Workplane("XY")
            .workplane(offset=params.base_thickness)
            .pushPoints(standoff_points)
            .circle(params.standoff_diameter / 2.0)
            .extrude(params.standoff_height)
        )
        enclosure = enclosure.union(standoffs)

        screw_holes = (
            cq.Workplane("XY")
            .workplane(offset=params.base_thickness - 0.01)
            .pushPoints(standoff_points)
            .circle(params.screw_hole_diameter / 2.0)
            .extrude(params.standoff_height + 0.5)
        )
        enclosure = enclosure.cut(screw_holes)

    for cutout in cutouts:
        local_x = inner_cx + cutout.center_xy[0]
        local_y = inner_cy + cutout.center_xy[1]
        z_bottom = params.base_thickness + params.standoff_height + cutout.z_bottom
        z_bottom = max(0.4, z_bottom)

        if cutout.side == "x-":
            cutter = (
                cq.Workplane("XY")
                .transformed(offset=(params.wall_thickness / 2.0, local_y, z_bottom))
                .box(
                    params.wall_thickness + 0.8,
                    cutout.width_xy,
                    cutout.height_z,
                    centered=(True, True, False),
                )
            )
        elif cutout.side == "x+":
            cutter = (
                cq.Workplane("XY")
                .transformed(offset=(outer_w - params.wall_thickness / 2.0, local_y, z_bottom))
                .box(
                    params.wall_thickness + 0.8,
                    cutout.width_xy,
                    cutout.height_z,
                    centered=(True, True, False),
                )
            )
        elif cutout.side == "y-":
            cutter = (
                cq.Workplane("XY")
                .transformed(offset=(local_x, params.wall_thickness / 2.0, z_bottom))
                .box(
                    cutout.width_xy,
                    params.wall_thickness + 0.8,
                    cutout.height_z,
                    centered=(True, True, False),
                )
            )
        else:
            cutter = (
                cq.Workplane("XY")
                .transformed(offset=(local_x, outer_h - params.wall_thickness / 2.0, z_bottom))
                .box(
                    cutout.width_xy,
                    params.wall_thickness + 0.8,
                    cutout.height_z,
                    centered=(True, True, False),
                )
            )

        enclosure = enclosure.cut(cutter)

    # Center model in XY so slicers with different bed-origin assumptions place it consistently.
    enclosure = enclosure.translate((-outer_w / 2.0, -outer_h / 2.0, 0.0))
    exporters.export(enclosure, str(output_stl))

    print(f"Input PCB STEP: {step_path}")
    print(f"Output enclosure STL: {output_stl}")
    print(f"Output standoff template PDF: {output_pdf}")
    if debug_holes_csv:
        print(f"Output hole debug CSV: {debug_holes_csv}")
    print(f"Detected mounting holes: {len(holes)}")
    print(f"Detected wall cutouts: {len(cutouts)}")
    print(f"PCB size (mm): {pcb_w:.2f} x {pcb_h:.2f}")
    if base_only:
        print("Mode: base-only (no walls)")
    else:
        print(f"Wall height (mm): {wall_height:.2f} (includes +{params.top_margin:.1f} mm margin)")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate a PCB enclosure STL from a STEP model using CadQuery."
    )
    parser.add_argument("pcb_step", nargs="?", type=Path, help="Path to PCB STEP file")
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=None,
        help="Output STL path",
    )
    parser.add_argument(
        "--layout-yaml",
        type=Path,
        default=None,
        help="Compatibility mode: source YAML layout analyzed then built via intermediate file.",
    )
    parser.add_argument(
        "--analyze-layout",
        type=Path,
        default=None,
        help="Analyze source layout YAML and write intermediate YAML feature file.",
    )
    parser.add_argument(
        "--build-from-intermediate",
        type=Path,
        default=None,
        help="Build STL and template PDF from intermediate YAML feature file.",
    )
    parser.add_argument(
        "--intermediate-output",
        type=Path,
        default=None,
        help="Output path for intermediate YAML (used with --analyze-layout or --layout-yaml).",
    )
    parser.add_argument(
        "--pdf-output",
        type=Path,
        default=None,
        help="Output PDF path for 1:1 standoff template (default: <input stem>_standoff_template.pdf)",
    )
    parser.add_argument(
        "--panel-local-holes-csv",
        type=Path,
        default=None,
        help="Output CSV path for board-local mounting-hole coordinates in panel mode.",
    )
    parser.add_argument(
        "--debug-holes-csv",
        type=Path,
        default=None,
        help="Write CSV with accepted/rejected circular hole candidates.",
    )
    parser.add_argument(
        "--base-only",
        action="store_true",
        help="Generate only the base and standoffs (no walls or wall cutouts).",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    params = Params()

    if args.analyze_layout:
        layout_yaml = args.analyze_layout
        if not layout_yaml.exists():
            raise SystemExit(f"Layout YAML not found: {layout_yaml}")

        intermediate_output = args.intermediate_output or layout_yaml.with_name(
            f"{layout_yaml.stem}_intermediate.yaml"
        )
        output_local_holes_csv = args.panel_local_holes_csv or layout_yaml.with_name(
            f"{layout_yaml.stem}_local_holes.csv"
        )
        analyze_layout_to_intermediate(layout_yaml, intermediate_output, output_local_holes_csv, params)
        return

    if args.build_from_intermediate:
        intermediate_yaml = args.build_from_intermediate
        if not intermediate_yaml.exists():
            raise SystemExit(f"Intermediate YAML not found: {intermediate_yaml}")

        output = args.output or intermediate_yaml.with_name(f"{intermediate_yaml.stem}_panel.stl")
        output_pdf = args.pdf_output or intermediate_yaml.with_name(
            f"{intermediate_yaml.stem}_panel_template.pdf"
        )
        build_from_intermediate(intermediate_yaml, output, output_pdf, params)
        return

    if args.layout_yaml:
        layout_yaml = args.layout_yaml
        if not layout_yaml.exists():
            raise SystemExit(f"Layout YAML not found: {layout_yaml}")

        intermediate_output = args.intermediate_output or layout_yaml.with_name(
            f"{layout_yaml.stem}_intermediate.yaml"
        )
        output = args.output or layout_yaml.with_name(f"{layout_yaml.stem}_panel.stl")
        output_pdf = args.pdf_output or layout_yaml.with_name(f"{layout_yaml.stem}_panel_template.pdf")
        output_local_holes_csv = args.panel_local_holes_csv or layout_yaml.with_name(
            f"{layout_yaml.stem}_local_holes.csv"
        )
        analyze_layout_to_intermediate(layout_yaml, intermediate_output, output_local_holes_csv, params)
        build_from_intermediate(intermediate_output, output, output_pdf, params)
        print("Compatibility mode: use --analyze-layout and --build-from-intermediate for explicit two-step flow.")
        return

    if not args.pcb_step:
        raise SystemExit("Provide pcb_step for single-board mode, or use --layout-yaml for panel mode")

    step_path = args.pcb_step
    if not step_path.exists():
        raise SystemExit(f"STEP file not found: {step_path}")

    output = args.output or step_path.with_name(f"{step_path.stem}_enclosure.stl")
    output_pdf = args.pdf_output or step_path.with_name(f"{step_path.stem}_standoff_template.pdf")
    debug_holes_csv = args.debug_holes_csv
    build_enclosure(
        step_path,
        output,
        output_pdf,
        params,
        debug_holes_csv=debug_holes_csv,
        base_only=args.base_only,
    )


if __name__ == "__main__":
    main()
