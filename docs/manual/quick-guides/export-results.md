# Quick guide: Exporting results

**You need:** an open dataset.

![Export Images](../images/export-images.png)

**Measurements**

1. Measure the cells (**Analysis → Measure Cells**).
2. Choose **I/O → Export → Measurements (CSV)...** and save the file.

**Images**

1. Choose **I/O → Export → Images (TIFF)...**.
2. Choose the **Output folder:**.
3. Tick the channels, segmentations and masks to export.
4. Click **Export**. Each item becomes `<dataset>_<item>.tif`.

**Phasor data**

1. Compute the phasor for each channel (**FLIM → Compute Phasor**).
2. Choose **I/O → Export → Phasor (.npz)...** and choose a folder.

**Many datasets:** use `percell-batch-export` and `percell-batch-measure` in [Batch Tools](../09-batch-tools.md).

More: [Exporting results](../03-datasets.md#exporting-results).
