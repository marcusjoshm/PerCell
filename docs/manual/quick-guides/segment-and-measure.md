# Quick guide: Segmenting and measuring cells

**You need:** an open dataset.

![The Segmentation tab](../images/tab-segmentation-cellpose.png)

1. In the Session bar, set **Channel:** to the channel that shows the cells best.
2. Open the **Segmentation** tab.
3. Set **Diameter (px):** to the typical cell diameter. Tick **Show diameter reference circle** to compare.
4. Click **Run Cellpose** and accept the name `cellpose`.
5. Check the cells in the viewer. Fix mistakes with **Delete Selected Label** and **Add New Label (next ID)**.
6. Open the **Analysis** tab and click **Measure Cells**. Tick the metrics and confirm.
7. Click **Open Cell Table** to see one row per cell.

**Result:** a segmentation named `cellpose` and a measurement table in the dataset.

More: [Segmentation](../05-segmentation.md), [Measuring cells](../06-analysis.md#measuring-cells).
