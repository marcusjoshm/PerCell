# Quick guide: Finding puncta

**You need:** an open dataset with a cell segmentation.

![Adaptive Local Clipping](../images/tab-analysis-alc-cnr-metric.png)

1. In the Session bar, set **Channel:** to the puncta channel and **Segmentation:** to the cells.
2. Open the **Analysis** tab.
3. Under **Adaptive Local Clipping**, set **Smallest Particle Diameter:** to the size of the smallest puncta.
4. Click **Run Adaptive Clipping** and accept the name `adaptive`. The mask becomes active.
5. Optional: to separate bright from dim puncta, set **Source mask:** to `adaptive` and click **Classify Mask by CNR**.
6. Under **Particle Analysis**, click **Analyze Particles**.
7. Under **Measurements**, click **Measure Cells**.
8. Open the Cell Table to see `particle_count` and the other particle columns for each cell.

**Result:** a puncta mask and per-cell particle measurements.

More: [Adaptive Local Clipping](../06-analysis.md#adaptive-local-clipping).
