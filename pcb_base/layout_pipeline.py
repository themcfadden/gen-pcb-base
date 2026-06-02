from __future__ import annotations

import math
from pathlib import Path
from typing import Any, Dict, List, Tuple

from .geometry import rotate_point, wrap_label_to_fit
from .io_outputs import load_yaml_file, write_panel_local_holes_csv, write_yaml_file
from .models import Params
from .progress import ProgressBar
from .step_analysis import analyze_board


def load_layout_yaml(layout_yaml: Path) -> Dict:
    data = load_yaml_file(layout_yaml)

    if "boards" not in data or not isinstance(data["boards"], list) or not data["boards"]:
        raise RuntimeError("Layout YAML must contain a non-empty 'boards' list")

    return data


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
    base_style = str(panel_cfg.get("base_style", "ribbed_islands")).strip().lower()
    base_frame_mm = float(panel_cfg.get("base_frame_mm", 8.0))
    base_rib_width_mm = float(panel_cfg.get("base_rib_width_mm", 3.2))
    base_rib_pitch_mm = float(panel_cfg.get("base_rib_pitch_mm", 24.0))
    base_island_padding_mm = float(panel_cfg.get("base_island_padding_mm", 6.0))
    _max_size_raw = panel_cfg.get("max_panel_size_mm", [250.0, 250.0])
    if not (isinstance(_max_size_raw, (list, tuple)) and len(_max_size_raw) == 2):
        raise RuntimeError("panel.max_panel_size_mm must be a list of [width, height]")
    max_panel_w = float(_max_size_raw[0])
    max_usable_w = max_panel_w - 2.0 * panel_margin

    if wall_height < 0.0:
        raise RuntimeError("panel.wall_height must be >= 0")
    if pdf_grid_minor_mm <= 0.0:
        raise RuntimeError("panel.pdf_grid_minor_mm must be > 0")
    if pdf_grid_major_mm <= 0.0:
        raise RuntimeError("panel.pdf_grid_major_mm must be > 0")
    if base_style not in ("solid", "ribbed_islands", "ribbed", "islands_connected"):
        raise RuntimeError("panel.base_style must be one of: solid, ribbed_islands, ribbed, islands_connected")
    if base_frame_mm < 0.0:
        raise RuntimeError("panel.base_frame_mm must be >= 0")
    if base_rib_width_mm <= 0.0:
        raise RuntimeError("panel.base_rib_width_mm must be > 0")
    if base_rib_pitch_mm <= 0.0:
        raise RuntimeError("panel.base_rib_pitch_mm must be > 0")
    if base_island_padding_mm < 0.0:
        raise RuntimeError("panel.base_island_padding_mm must be >= 0")

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

    auto_cursor_x = auto_origin_x
    auto_cursor_y = auto_origin_y
    row_max_h = 0.0
    place_bar = ProgressBar(len(board_inputs), "place")
    for board in board_inputs:
        angle = math.radians(float(board["rotation"]))
        footprint_w = abs(float(board["board_w"]) * math.cos(angle)) + abs(float(board["board_h"]) * math.sin(angle))
        footprint_h = abs(float(board["board_h"]) * math.cos(angle)) + abs(float(board["board_w"]) * math.sin(angle))

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
            "base_style": "ribbed_islands" if base_style in ("ribbed", "islands_connected") else base_style,
            "base_frame_mm": base_frame_mm,
            "base_rib_width_mm": base_rib_width_mm,
            "base_rib_pitch_mm": base_rib_pitch_mm,
            "base_island_padding_mm": base_island_padding_mm,
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
