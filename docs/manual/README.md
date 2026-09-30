# PerCell User Manual

PerCell is a desktop application for single-cell analysis of fluorescence microscopy and FLIM images. It segments cells with Cellpose, finds condensates and puncta by thresholding, measures every cell, and analyses fluorescence lifetime with the phasor method. Each experiment is stored as one HDF5 file (`.h5`), called a **dataset**.

This manual describes every window and control of PerCell and walks through the common tasks step by step.

## How this manual is organised

The manual follows the pattern of the Leica LAS X help. Each chapter covers one area of the application and has up to three kinds of section:

| Section | What it gives you |
|---|---|
| **Overview** | What the area is for and how it fits the rest of PerCell. |
| **Dialog descriptions** | A screenshot of each window or panel, then every group and control in the order you see them, with defaults and what they write to the dataset. |
| **Instructions** | Numbered steps for a task: "How to …". |

Short step lists for the most common jobs are collected as [Quick guides](quick-guides/README.md).

## Contents

1. [Overview](01-overview.md) — what PerCell does, its features, and how to install and start it.
2. [The user interface](02-user-interface.md) — the launcher, the Session bar, the viewer, and the other windows.
3. [Datasets: I/O and Data](03-datasets.md) — create, open, add to, export, and manage datasets.
4. [Viewing and selecting cells](04-viewer.md) — the napari viewer, cell selection and filters, the Data Plot and the Cell Table.
5. [Segmentation](05-segmentation.md) — Cellpose, tracking over time, and editing labels.
6. [Analysis](06-analysis.md) — thresholding, puncta detection, particle analysis, and measurements.
7. [FLIM](07-flim.md) — phasor analysis, filtering, phasor masks, and lifetime maps.
8. [Workflows and analyses](08-workflows.md) — the batch workflows and analyses that run over many datasets.
9. [Batch Tools](09-batch-tools.md) — run the `percell-*` command-line tools from inside PerCell.
10. [Advanced settings](10-advanced.md) — the Cellpose device and the PyTorch environment.
11. [Glossary](glossary.md)

## Conventions

- **Bold** text is a label you see on screen: a button, tab, group, or field.
- A path such as **I/O → Add Data → Layer...** means: open the **I/O** tab, click **Add Data**, then choose **Layer...**.
- Paths such as `/masks/<name>` name where PerCell stores a result inside the dataset file.
- "Active" channel, mask or segmentation means the one selected in the [Session bar](02-user-interface.md#the-session-bar). Most tools work on the active items.

> **Note:** Screenshots marked *Screenshot to come* are listed in the [screenshot list](shot-list.md).

## Related documents

- [Installation](../installation.md)
- [Command-line tools](../cli.md)
- [Changelog](../CHANGELOG.md)
