# Glossary

**Active channel, mask, segmentation**
The items chosen in the [Session bar](02-user-interface.md#the-session-bar). Most tools work on these.

**Adaptive Local Clipping (ALC)**
PerCell's puncta detection method. It finds particles cell by cell against a local background, with no hand-set threshold. See [Adaptive Local Clipping](06-analysis.md#adaptive-local-clipping).

**Calibration (φ, M)**
The phase and modulation of the FLIM instrument response, per channel and harmonic. Phasor analysis uses them to place the phasor correctly.

**Channel**
A named intensity image in a dataset: one stain, detector or band.

**CNR (contrast-to-noise ratio)**
How far a particle's peak stands above its local background, in units of the cell's noise. Used to separate bright, in-focus puncta from dim ones.

**Dataset**
One acquisition and everything derived from it, stored in one `.h5` file.

**Decay**
The FLIM photon-arrival histogram of each pixel, read from TCSPC `.bin` files.

**Dilute phase**
The part of a cell outside its condensates. A dilute-phase mask is the cell area minus the grown condensate mask.

**Edge cell**
A cell that touches, or lies near, the image edge. It is only partly imaged.

**GMM (Gaussian mixture model)**
A statistical fit that finds groups in data. Grouped thresholding uses it to group cells by brightness; phasor segmentation uses it to find lifetime clusters.

**Grouped thresholding**
Thresholding in which cells are first sorted into groups of similar brightness, and each group gets its own threshold.

**Harmonic**
Which frequency of the laser repetition rate the phasor is computed at. Harmonic 1 is standard.

**Mask**
A per-pixel yes/no image that marks which pixels are in play, for example condensates. A mask answers "where"; a segmentation answers "which cell".

**Phasor**
Two coordinates, G and S, computed from a pixel's decay. Pixels with the same lifetime fall on the same point of the phasor plot.

**Pixel binning**
Combining each N×N block of pixels into one when reading a dataset. Set in the Session bar; it never changes the file.

**Preview layer**
A temporary viewer layer, whose name starts with `_`, that a tool shows while you adjust it. It is never saved.

**ROI (phasor)**
A region drawn on the Phasor Plot to select pixels by lifetime. Applying it makes a mask.

**Round**
One thresholding pass in the single-cell workflow. Each round makes one mask.

**Segmentation**
An image in which each cell has its own number (a label). Made by Cellpose, imported, or drawn by hand.

**Tracking**
Linking the cells of a time-lapse segmentation so each cell keeps one number over time.
