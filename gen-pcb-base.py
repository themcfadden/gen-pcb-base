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
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

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
    standoff_holes: List[Tuple[float, float, float]],
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

    for x, y, d in standoff_holes:
        hole = patches.Circle((x, y), d / 2.0, linewidth=0.8, edgecolor="black", facecolor="none")
        ax.add_patch(hole)
        mark = 1.2
        ax.plot([x - mark, x + mark], [y, y], color="black", linewidth=0.5)
        ax.plot([x, x], [y - mark, y + mark], color="black", linewidth=0.5)

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


def load_layout_yaml(layout_yaml: Path) -> Dict:
    try:
        import yaml
    except Exception as exc:
        raise RuntimeError(
            "PyYAML is required for --layout-yaml. Install with: pip install pyyaml"
        ) from exc

    with layout_yaml.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f)

    if not isinstance(data, dict):
        raise RuntimeError("Layout YAML root must be a mapping")
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

    local_holes = [(x - pcb_cx, y - pcb_cy, d) for x, y, d in holes]

    return {
        "pcb_w": pcb_w,
        "pcb_h": pcb_h,
        "local_holes": local_holes,
    }


def build_panel(
    layout_yaml: Path,
    output_stl: Path,
    output_pdf: Path,
    output_local_holes_csv: Path,
    params: Params,
) -> None:
    data = load_layout_yaml(layout_yaml)

    panel_cfg = data.get("panel", {})
    if not isinstance(panel_cfg, dict):
        raise RuntimeError("'panel' must be a mapping when provided")

    panel_margin = float(panel_cfg.get("margin", 8.0))
    base_thickness = float(panel_cfg.get("base_thickness", params.base_thickness))
    standoff_height = float(panel_cfg.get("standoff_height", params.standoff_height))
    standoff_diameter = float(panel_cfg.get("standoff_diameter", params.standoff_diameter))
    screw_hole_diameter = float(panel_cfg.get("screw_hole_diameter", params.screw_hole_diameter))
    exclude_xy_tolerance = float(panel_cfg.get("exclude_xy_tolerance", 0.08))

    placements = []
    all_outline_pts = []
    local_hole_rows: List[dict] = []

    for idx, board in enumerate(data["boards"]):
        if not isinstance(board, dict):
            raise RuntimeError(f"boards[{idx}] must be a mapping")

        step_val = board.get("step")
        if not step_val:
            raise RuntimeError(f"boards[{idx}] is missing required field: step")

        step_path = (layout_yaml.parent / str(step_val)).resolve()
        if not step_path.exists():
            raise RuntimeError(f"boards[{idx}] STEP not found: {step_path}")

        name = str(board.get("name", f"board_{idx + 1}"))
        label = str(board.get("label", step_path.stem))
        x = float(board.get("x", 0.0))
        y = float(board.get("y", 0.0))
        rotation = float(board.get("rotation", 0.0))
        board_standoff_diameter = float(board.get("standoff_diameter", standoff_diameter))
        board_screw_hole_diameter = float(board.get("screw_hole_diameter", screw_hole_diameter))

        analyzed = analyze_board(step_path, params)
        w = analyzed["pcb_w"]
        h = analyzed["pcb_h"]
        local_holes_all = sorted(analyzed["local_holes"], key=lambda hole: (hole[1], hole[0]))

        exclusion_rules = board.get("exclude_standoffs", [])
        if exclusion_rules is None:
            exclusion_rules = []
        if not isinstance(exclusion_rules, list):
            raise RuntimeError(f"boards[{idx}].exclude_standoffs must be a list")

        local_holes_kept, excluded_map = apply_standoff_exclusions(
            local_holes_all,
            exclusion_rules,
            exclude_xy_tolerance,
        )

        for hole_index, (hx, hy, hd) in enumerate(local_holes_all):
            reason = excluded_map.get(hole_index, "")
            local_hole_rows.append(
                {
                    "board": name,
                    "step": step_path.name,
                    "hole_index": hole_index,
                    "x_local_mm": f"{hx:.4f}",
                    "y_local_mm": f"{hy:.4f}",
                    "diameter_mm": f"{hd:.4f}",
                    "excluded": "yes" if hole_index in excluded_map else "no",
                    "reason": reason,
                }
            )

        corners_local = [
            (-w / 2.0, -h / 2.0),
            (w / 2.0, -h / 2.0),
            (w / 2.0, h / 2.0),
            (-w / 2.0, h / 2.0),
        ]
        corners_world = []
        for cx, cy in corners_local:
            rx, ry = rotate_point(cx, cy, rotation)
            wx = x + rx
            wy = y + ry
            corners_world.append((wx, wy))
            all_outline_pts.append((wx, wy))

        hole_points = []
        hole_draw = []
        for hx, hy, d in local_holes_kept:
            rx, ry = rotate_point(hx, hy, rotation)
            wx = x + rx
            wy = y + ry
            hole_points.append((wx, wy))
            hole_draw.append((wx, wy, d))

        placements.append(
            {
                "name": name,
                "label": label,
                "board_w": w,
                "board_h": h,
                "x": x,
                "y": y,
                "rotation": rotation,
                "standoff_diameter": board_standoff_diameter,
                "screw_hole_diameter": board_screw_hole_diameter,
                "detected_holes": len(local_holes_all),
                "excluded_holes": len(excluded_map),
                "active_holes": len(local_holes_kept),
                "corners_world": corners_world,
                "holes_xy": hole_points,
                "holes_draw": hole_draw,
            }
        )

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

    panel = cq.Workplane("XY").box(
        panel_w,
        panel_h,
        base_thickness,
        centered=(False, False, False),
    )

    all_standoff_points = []
    pdf_outlines = []
    pdf_holes = []

    for placement in placements:
        outline = [to_panel(x, y) for x, y in placement["corners_world"]]
        pdf_outlines.append(outline)

        for x, y in placement["holes_xy"]:
            sx, sy = to_panel(x, y)
            all_standoff_points.append((sx, sy))

        placement["holes_panel_xy"] = [to_panel(x, y) for x, y in placement["holes_xy"]]

        for x, y, d in placement["holes_draw"]:
            sx, sy = to_panel(x, y)
            pdf_holes.append((sx, sy, d))

    # Emboss board label under each board, wrapping text to stay inside footprint.
    for placement in placements:
        label = placement["label"]
        lines, font_size = wrap_label_to_fit(label, placement["board_w"], placement["board_h"], params)
        line_pitch = font_size * 1.28
        total_height = line_pitch * (len(lines) - 1)
        tx, ty = to_panel(placement["x"], placement["y"])

        for i, line in enumerate(lines):
            y_offset = -total_height / 2.0 + i * line_pitch
            text_wp = (
                cq.Workplane("XY")
                .transformed(offset=(tx, ty, base_thickness), rotate=(0, 0, placement["rotation"]))
                .center(0.0, y_offset)
                .text(
                    line,
                    fontsize=font_size,
                    distance=params.label_height,
                    combine=False,
                    halign="center",
                    valign="center",
                )
            )
            panel = panel.union(text_wp)

    if all_standoff_points:
        for placement in placements:
            board_points = placement.get("holes_panel_xy", [])
            if not board_points:
                continue

            board_standoff_diameter = float(
                placement.get("standoff_diameter", standoff_diameter)
            )
            board_screw_hole_diameter = float(
                placement.get("screw_hole_diameter", screw_hole_diameter)
            )

            standoffs = (
                cq.Workplane("XY")
                .workplane(offset=base_thickness)
                .pushPoints(board_points)
                .circle(board_standoff_diameter / 2.0)
                .extrude(standoff_height)
            )
            panel = panel.union(standoffs)

            screw_holes = (
                cq.Workplane("XY")
                .workplane(offset=base_thickness - 0.01)
                .pushPoints(board_points)
                .circle(board_screw_hole_diameter / 2.0)
                .extrude(standoff_height + 0.5)
            )
            panel = panel.cut(screw_holes)

    exporters.export(panel, str(output_stl))
    write_panel_standoff_template_pdf(output_pdf, panel_w, panel_h, pdf_outlines, pdf_holes)
    write_panel_local_holes_csv(output_local_holes_csv, local_hole_rows)

    print(f"Input layout YAML: {layout_yaml}")
    print(f"Output panel STL: {output_stl}")
    print(f"Output panel template PDF: {output_pdf}")
    print(f"Output local holes CSV: {output_local_holes_csv}")
    print(f"Boards placed: {len(placements)}")
    print(f"Total standoffs: {len(all_standoff_points)}")
    print(f"Panel size (mm): {panel_w:.2f} x {panel_h:.2f}")
    print("Board local holes:")
    for placement in placements:
        print(
            "  "
            f"{placement['name']}: "
            f"detected={placement['detected_holes']}, "
            f"excluded={placement['excluded_holes']}, "
            f"active={placement['active_holes']}"
        )
    for row in local_hole_rows:
        print(
            "  "
            f"{row['board']} idx={row['hole_index']} "
            f"xy=({row['x_local_mm']}, {row['y_local_mm']}) "
            f"d={row['diameter_mm']} "
            f"excluded={row['excluded']} {row['reason']}"
        )


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
        help="YAML layout for multi-board panel mode.",
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

    if args.layout_yaml:
        layout_yaml = args.layout_yaml
        if not layout_yaml.exists():
            raise SystemExit(f"Layout YAML not found: {layout_yaml}")

        output = args.output or layout_yaml.with_name(f"{layout_yaml.stem}_panel.stl")
        output_pdf = args.pdf_output or layout_yaml.with_name(f"{layout_yaml.stem}_panel_template.pdf")
        output_local_holes_csv = args.panel_local_holes_csv or layout_yaml.with_name(
            f"{layout_yaml.stem}_local_holes.csv"
        )
        build_panel(layout_yaml, output, output_pdf, output_local_holes_csv, params)
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
