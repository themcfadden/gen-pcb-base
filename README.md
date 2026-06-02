# PCB Enclosure Generator

This repository contains a Python script for generating 3D printable panel and enclosure STL files from PCB STEP models using CadQuery.

## Overview

The script supports both classic single-board generation and multi-board layout generation.

Multi-board mode now uses a two-step workflow:

1. Analyze source layout YAML and generate an intermediate YAML feature file.
2. Build STL/PDF from the intermediate YAML.

This makes detected geometry editable before final build, including standoffs, labels, and wall cutouts.

## Features

- Automatic PCB mounting-hole detection and standoff generation
- Connector wall-cutout detection from STEP solids near PCB edges
- Per-board overrides for:
  - standoff height
  - standoff diameter
  - screw-hole diameter
  - label text and label orientation
- Global panel and wall settings, including wall height
- Auto-placement with row wrapping constrained by max panel size
- `wall_height: 0` support for panel/base-only output
- Intermediate YAML with explicit feature toggles (`enabled: true/false`)
- Local-only intermediate feature coordinates (`x_local_mm`, `y_local_mm`)
- 1:1 PDF template generation for fit checks
- Optional coordinate grid and board-center coordinate annotations in panel PDF output
- CSV output of board-local mounting-hole coordinates
- STL export centered in XY (origin at model center) for more consistent slicer placement

## Prerequisites

- Python 3.7+
- CadQuery 2.0+
- PyYAML
- Matplotlib

## Install

```bash
pip install cadquery pyyaml matplotlib
```

## Layout YAML

See `panel_layout_example.yaml` for a complete example.

Top-level sections:

- `panel`: global defaults and enclosure/panel settings
- `boards`: list of board entries (`kind: step` or `kind: template`) and per-board overrides

Important `panel` fields:

- `margin`
- `base_thickness`
- `wall_height` (`0` means no enclosure walls)
- `wall_thickness`
- `standoff_height` (default)
- `standoff_diameter` (default)
- `screw_hole_diameter` (default)
- `label_height`
- `exclude_xy_tolerance`
- `max_panel_size_mm: [width, height]` (auto-placement bound; default `[250.0, 250.0]`)
- `auto_gap_x`, `auto_gap_y` (board spacing during auto-placement)
- `auto_origin_x`, `auto_origin_y` (auto-placement starting point)
- `pdf_grid_enabled`, `pdf_grid_minor_mm`, `pdf_grid_major_mm` (panel PDF grid overlay)

Per-board fields (under `boards[]`):

- Common: `kind`, `rotation`
- `label`, `label_rotation`, `label_enabled`
- `standoff_height`, `standoff_diameter`, `screw_hole_diameter`

Standoff exclusion is handled only in the intermediate YAML by setting
`boards[].features.standoffs[].enabled: false`.

Board kind-specific fields:

- `kind: step`
  - required: `step`
  - standoffs/cutouts are auto-detected from STEP
- `kind: template`
  - required: `size_mm: [width, height]`
  - optional manual `standoffs` list (`xy`, optional `hole_diameter`)
  - optional manual `wall_cutouts` list (`side`, `xy`, `width_mm`, `height_mm`, optional `z_bottom_mm`)

Board `x`/`y` in source layout YAML are ignored. Positions are auto-generated during the analyze step and written to intermediate YAML (`x_mm`, `y_mm`), where they can be edited before build. Stage 2 requires `x_mm` and `y_mm` to be present.

## Two-Step Workflow (Recommended)

### 1. Analyze source layout to intermediate YAML

```bash
python gen-pcb-base.py \
  --analyze-layout panel_layout_example.yaml \
  --intermediate-output panel_layout_intermediate.yaml
```

This writes:

- intermediate YAML feature file
- local-hole CSV (default: `<layout>_local_holes.csv`)

### 2. Edit intermediate YAML (optional)

You can disable features before final build by setting:

- `boards[].features.standoffs[].enabled: false`
- `boards[].features.wall_cutouts[].enabled: false`
- `boards[].features.label.enabled: false`

Board placement is controlled by:

- `boards[].x_mm`
- `boards[].y_mm`

Feature placement is local to each board:

- standoffs: `boards[].features.standoffs[].x_local_mm`, `y_local_mm`
- cutouts: `boards[].features.wall_cutouts[].x_local_mm`, `y_local_mm`
- label: `boards[].features.label.x_local_mm`, `y_local_mm`

During build, local feature coordinates are transformed by board center (`x_mm`, `y_mm`) and board rotation (`rotation_deg`).

### 3. Build from intermediate YAML

```bash
python gen-pcb-base.py \
  --build-from-intermediate panel_layout_intermediate.yaml \
  --output panel_layout_panel.stl \
  --pdf-output panel_layout_template.pdf
```

## Compatibility Mode

This still works and runs both steps automatically:

```bash
python gen-pcb-base.py --layout-yaml panel_layout_example.yaml
```

## Single-Board Mode

Single-board mode is unchanged:

```bash
python gen-pcb-base.py ARK_PAB_Carrier_3D_Model.step
```

