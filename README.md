<img src="arcform.png" alt="Arcform logo" width="128">

# Arcform

Convert, shrink, copy and merge files straight from the Windows right-click menu.

On Windows 11, Arcform is under **Show more options** (or hold Shift while right-clicking).

## What's in the menu

| Right-click a… | Convert to | Copy as | Shrink to fit |
|---|---|---|---|
| `.docx` / `.doc` | PDF | PDF | |
| `.pdf` | Word (.docx) | Word | |
| `.png` | JPG, JPEG, WEBP | JPG, JPEG, WEBP | ✓ |
| `.jpg` / `.jpeg` | PNG, the other JPEG name, WEBP | PNG, WEBP | ✓ |
| `.webp` | PNG, JPG | PNG, JPG | ✓ |
| `.heic` / `.heif` (iPhone photos) | JPG, PNG | JPG, PNG | ✓ |
| `.mp4` | MP3 (audio) | MP3 | ✓ |
| `.mov` / `.mkv` / `.webm` / `.avi` | MP4, MP3 (audio) | MP4, MP3 | ✓ |

PDFs also have **Split PDF ▸** and **Add to merge queue** (see below).

- **Convert to** saves the new file next to the original. Nothing is overwritten:
  if `photo.png` already exists you get `photo (1).png`. Photos are turned upright
  if the phone saved them sideways. Videos that are already H.264 are repackaged
  into MP4 in seconds without losing quality; other videos are re-encoded.
- **Copy as ▸** converts the file and puts the result on the clipboard instead of
  saving it, so you can paste it straight into a chat, an email, a website's upload
  box or a folder. Images are also copied as a picture for apps like Paint.
- **Shrink to fit ▸** makes a smaller copy under **1 MB** (web forms), **10 MB**
  (Discord) or **25 MB** (email), named like `video (under 10 MB).mp4`.
  - Images are saved as JPG (or WEBP if they have transparency) at the best quality that fits.
    They're only made smaller in size if quality alone isn't enough. Photo
    metadata such as location is removed.
  - Videos are re-encoded to exactly fit, lowering the resolution only when needed.

### Selecting several files

Select as many files as you like and pick one action. They all appear in **one
Arcform window** with a progress bar, instead of one window each. Files copied
together with **Copy as** go on the clipboard together. Closing the window
mid-job asks first, then stops cleanly without leaving half-written files.

Images are processed several at a time. Word documents, PDFs and videos go one
at a time (Word and the PDF library can only do one at once, and video encoding
already uses every CPU core). Word stays open between documents in a batch and
quits a few seconds after the last one.

## Splitting PDFs

Right-click a PDF → **Arcform → Split PDF ▸**. The parts go into a new folder
next to it, such as `report (split)`.

- **Choose where to cut…** shows every page as a thumbnail. Click the ✂ between
  two pages to cut there, and click a page to leave it out. Each resulting file
  gets its own colour, so you can see exactly what goes where before splitting.
  Quick cuts do the clicking for you: every page, every N pages, at chapters,
  or at blank pages.
- **By chapters** uses the PDF's bookmarks, naming each file after its chapter
  (`01 - Introduction.pdf`, …).
- **At blank pages** is for a stack of documents scanned in one go with blank
  sheets between them. It finds the blank sheets, drops them, and saves each
  document separately.
- **Into email-sized parts** makes the fewest parts that are each under 25 MB.
- **Every page** saves each page as its own PDF.

## Merging PDFs

1. Right-click a PDF → **Arcform → Add to merge queue**. Repeat for each PDF
   (or select several and add them at once).
2. Right-click empty space in the folder where you want the result (or any file
   Arcform supports) → **Arcform → Generate merge (N PDFs)**.

This saves `Merged.pdf` there and empties the queue.

**View merge queue** opens an editor where you can drag PDFs into order, move or
remove them, add more, and merge. It shows page counts and flags files that have
been moved or deleted. **Clear merge queue** starts over.

PDFs are merged in the order you added them. PDFs added together in one
multi-select are put in name order. The queue is stored in
`%LOCALAPPDATA%\Arcform\merge_queue.json`.

## Setup

```bash
python -m pip install -r requirements.txt
```

```bash
python install.py
```

To remove the menu:

```bash
python install.py uninstall
```

If you move this folder or reinstall Python, run `python install.py` again.

## Requirements

- **Word → PDF** uses Microsoft Word if it's installed, otherwise LibreOffice.
- **Video and audio** use ffmpeg from PATH, or the copy bundled with `imageio-ffmpeg`.

## Command line

Add `--no-gui` to print the result instead of opening a window.

```bash
python arcform.py convert pdf "report.docx" --no-gui
```

```bash
python arcform.py shrink 10 "clip.mov" --no-gui
```

```bash
python arcform.py copy jpg "IMG_0042.heic" --no-gui
```

```bash
python arcform.py split chapters "handbook.pdf" --no-gui
```

```bash
python arcform.py merge-generate "C:\some\folder" --no-gui
```

Unexpected errors are written to `arcform_error.log` in this folder.

## How it's put together

| File | Does |
|---|---|
| `arcform.py` | Entry point the right-click menu runs |
| `converters.py` | Format conversions, ffmpeg helpers |
| `shrink.py` | Shrink to fit |
| `clipboard.py` | Putting files on the clipboard |
| `split.py` | Splitting PDFs |
| `merge_queue.py` | The PDF merge queue |
| `tasks.py` | Jobs, and handing them to one shared window over a named pipe |
| `window.py` | The job window, the merge queue editor and the PDF cutter |
| `menu.py` | Registry entries for the right-click menu |
| `install.py` | Adds or removes the menu |
