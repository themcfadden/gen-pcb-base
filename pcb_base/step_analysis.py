from __future__ import annotations

import math
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import cadquery as cq

from .models import Params, SideCutout


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
