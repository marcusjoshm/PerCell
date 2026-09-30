# Quick guide: Making lifetime masks

**You need:** an open dataset with FLIM decay data for the channel.

![The FLIM tab](../images/tab-flim-phasor.png)

1. In the Session bar, set **Channel:** to the FLIM channel.
2. Open the **FLIM** tab. Check **Cal φ:** and **M:**.
3. Click **Compute Phasor**. The Phasor Plot opens.
4. Optional: click **Apply Wavelet Filter**, then tick **Wavelet filter** on the plot.
5. Set **Intensity ≥** to hide background pixels.
6. Click **Run GMM** to fit ROIs to the lifetime clusters, or **Add ROI** to place them by hand.
7. Adjust and rename the ROIs on the plot. The viewer previews each one.
8. Click **Apply ROIs as Masks**.
9. Click **Save ROIs...** to reuse the same ROIs on other datasets.

**Result:** one mask per ROI, named after the ROI.

More: [FLIM](../07-flim.md). For many datasets: [Automated phasor-masks workflow](../08-workflows.md#automated-phasor-masks-workflow).
