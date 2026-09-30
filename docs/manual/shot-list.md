# Screenshot list

These screenshots are still to be taken. Each chapter shows a *Screenshot to come* note where the picture belongs.

## How to take them

1. Open a dataset that has channels, a segmentation, at least one mask, measurements, and FLIM decay data, so every window has content to show.
2. Set the window into the state described below.
3. Press **Shift-Command-4**, then **Space**, and click the window. macOS saves it with its frame and shadow, like the existing screenshots.
4. Rename the file to the name in the first column.

Existing screenshots are stored at half their retina size. After adding new ones, resize them the same way, for example:

```bash
sips --resampleWidth $(( $(sips -g pixelWidth shot.png | awk '/pixelWidth/{print $2}') / 2 )) shot.png
```

Then replace the chapter's *Screenshot to come* note with the image link.

## Screenshots needed

| File | Chapter | Window | How to open it | What it should show |
|---|---|---|---|---|
