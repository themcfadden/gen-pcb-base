from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import cadquery as cq
from cadquery import exporters

from .geometry import _compute_board_keep_rect, _make_ribbed_cut_cells, rotate_point, rotate_side
from .io_outputs import (
    load_yaml_file,
    write_hole_debug_csv,
    write_panel_standoff_template_pdf,
    write_standoff_template_pdf,
)
from .layout_pipeline import analyze_layout_to_intermediate
from .models import Params
from .progress import ProgressBar
from .step_analysis import (
    detect_connector_cutouts,
    extract_circles,
    find_through_holes,
    largest_solid,
    top_bottom_faces,
)


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
    base_style = str(panel_cfg.get("base_style", "ribbed_islands")).strip().lower()
    if base_style in ("ribbed", "islands_connected"):
        base_style = "ribbed_islands"
    base_frame_mm = float(panel_cfg.get("base_frame_mm", 8.0))
    base_rib_width_mm = float(panel_cfg.get("base_rib_width_mm", 3.2))
    base_rib_pitch_mm = float(panel_cfg.get("base_rib_pitch_mm", 24.0))
    base_island_padding_mm = float(panel_cfg.get("base_island_padding_mm", 6.0))

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

    if base_style == "ribbed_islands":
        keep_rects: List[Tuple[float, float, float, float]] = []
        for board in boards:
            if not isinstance(board, dict):
                continue
            rect = _compute_board_keep_rect(board, base_island_padding_mm, panel_w, panel_h)
            if rect is not None:
                keep_rects.append(rect)

        cut_cells = _make_ribbed_cut_cells(
            panel_w,
            panel_h,
            base_frame_mm,
            base_rib_width_mm,
            base_rib_pitch_mm,
            keep_rects,
        )
        for x0, x1, y0, y1 in cut_cells:
            w = x1 - x0
            h = y1 - y0
            if w <= 0.2 or h <= 0.2:
                continue
            cutter = (
                cq.Workplane("XY")
                .transformed(offset=(panel_x_offset + x0, panel_y_offset + y0, -0.05))
                .box(w, h, base_thickness + 0.1, centered=(False, False, False))
            )
            model = model.cut(cutter)

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
