from __future__ import annotations

import csv
from pathlib import Path
from typing import Any, Dict, List, Tuple

from .geometry import rotate_point


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

    pcb_rect = patches.Rectangle(
        (margin, margin),
        pcb_w,
        pcb_h,
        linewidth=0.8,
        edgecolor="black",
        facecolor="none",
    )
    ax.add_patch(pcb_rect)

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
