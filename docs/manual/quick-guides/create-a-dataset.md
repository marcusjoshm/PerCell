# Quick guide: Creating a dataset

**You need:** a folder of TIFFs exported from the microscope.

![Compress TIFF Dataset](../images/compress-tiff-dataset.png)

1. Start PerCell with `percell`.
2. On the **I/O** tab, click **New Dataset...**.
3. Click **Browse...** next to **Directory:** and choose the folder of TIFFs.
4. Set **Discovery:**
   - **Subdirectory** if each subfolder is one dataset,
   - **Flat Directory** if all files are in one folder with `_ch00`-style names,
   - **Tokenless (by name)** if the channel is a name at the end of each file name.
5. Check the **Datasets** and **Channels** lists. Uncheck anything you do not want.
6. For z-stacks, choose **Z-Projection:** (**mip** is standard).
7. For tile scans, check the **Tile Stitching** settings and the tile-order preview.
8. Click **Compress**.
9. Click **Open Dataset...** and open the new `.h5` file.

**Result:** one `.h5` file per dataset, next to the TIFF folder unless you changed **Output:**.

More: [Creating a dataset from TIFFs](../03-datasets.md#creating-a-dataset-from-tiffs).
