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
from pathlib import Path
from pcb_base.builders import build_enclosure, build_from_intermediate
from pcb_base.layout_pipeline import analyze_layout_to_intermediate
from pcb_base.models import Params


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
