# PCB Enclosure Generator

This repository contains a Python script for generating 3D printable enclosure STL files from PCB STEP models using CADQuery.

## Overview

The `gen-pcb-base.py` script automates the creation of custom PCB enclosures by:
- Reading PCB STEP files
- Detecting mounting holes and creating matching standoffs
- Setting wall height to 5mm above the tallest component
- Detecting edge connector solids and creating wall cutouts
- Supporting both single-board and multi-board panel modes

## Features

- Automatic mounting hole detection and standoff generation
- Wall cutout detection for edge connectors
- Configurable parameters for enclosure dimensions
- PDF template generation for standoff placement
- CSV output for debugging and analysis
- Support for multi-board panel layouts via YAML configuration

## Getting Started

### Prerequisites

- Python 3.7+
- CadQuery 2.0+
- PyYAML (for panel mode)
- Matplotlib (for PDF template generation)

### Installation

