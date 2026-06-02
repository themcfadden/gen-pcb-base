from __future__ import annotations

from dataclasses import dataclass
from typing import Tuple


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
