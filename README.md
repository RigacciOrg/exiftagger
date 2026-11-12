# ExifTagger

ExifTagger is a lightweight graphical tool whose main purpose is
to add three kinds of metadata to a JPEG image: a brief
**caption** (useful, for example, as a slideshow title), a list
of **keywords**, and a **long description** (useful for
classifying and searching an image collection).

ExifTagger processes one image at a time, taking the image
filename as a command-line argument. This makes it suitable as
an external editor or plugin for image browsers (such as Geeqie
or gThumb).

ExifTagger is designed to complement image management software,
providing a focused metadata-editing tool rather than attempting to
replace a full-featured image organizer.

The three fields are stored in well-known EXIF and XMP tags,
ensuring they remain accessible to other software. Keywords are
also cached in a personal keyword vocabulary, making it easy to
reuse terms consistently throughout an image archive.

All metadata reading and writing is handled by **exiv2**, a
mature and reliable command-line tool. Written in Python with Qt
(PyQt6), ExifTagger is primarily intended for GNU/Linux, but can
also run on other operating systems where Python, PyQt6, and
exiv2 are available.

## Features

- Edit three logical fields of a single JPEG:
  - **Caption** — written to `Exif.Photo.UserComment`,
    `Exif.Image.ImageDescription` and `Xmp.dc.description` (kept in sync), and
    read back in that order of precedence.
  - **Keywords** — `Xmp.dc.subject`, shown as removable chips, with live
    suggestions from a local vocabulary.
  - **Long description** — `Xmp.iptc.ExtDescrAccessibility`.
- **UserComment decoding helper** for archive photos whose fields were written
  incorrectly: an alternative-encoding selector (UTF-16 LE/BE, UTF-8, ASCII,
  ISO-2022-JP, Latin-1, Windows-1252, ISO-8859-15, Mac Roman) and automatic
  flagging of malformed fields.
- **Local keyword vocabulary** with accent- and case-insensitive search
  (Unicode NFC/NFKD keys) that grows as you tag.
- **Robust handling of damaged metadata**: an invalid UserComment or a corrupt
  XMP packet is flagged in the interface (red border and a warning) instead of
  being silently lost, and unreadable files are reported rather than ignored.
  When such a problem is detected a write-once `<file>.orig` backup of the
  untouched original is forced before saving; a backup can also be requested
  explicitly with `--backup`, so overwriting damaged data is always recoverable.
- **Keyboard-friendly**: Alt-key mnemonics for every field, arrow-key suggestion
  picking, Esc to cancel, and an unsaved-changes confirmation.
- **Command-line actions** that do not import Qt, so they run over SSH on a
  headless host: build the vocabulary from a photo tree, and synchronise the
  three caption tags.
- **Free and Open Source Software** (GPL-3.0-or-later): a single lightweight
  Python script with no heavy dependencies beyond Qt and exiv2.

## Requirements

- **Python 3** (3.8 or newer).
- **PyQt6** — needed only for the graphical editor; the CLI actions run without it.
- **exiv2** — the external command used to read and write the metadata; tested
  against versions 0.27 and 0.28.

Install on common distributions:

```sh
# Debian / Ubuntu
sudo apt install exiv2 python3-pyqt6

# Fedora
sudo dnf install exiv2 python3-pyqt6

# Arch Linux
sudo pacman -S exiv2 python-pyqt6
```

(or `pip install PyQt6` inside a virtualenv). ExifTagger targets **GNU/Linux**,
but as it relies only on Python, Qt and the `exiv2` CLI it should be portable to
any platform where those are available.

## Usage

```sh
# Graphical editor on one image
python3 exiftagger.py photo.jpg

# Keep a write-once photo.jpg.orig backup before saving
python3 exiftagger.py photo.jpg --backup

# Build the keyword vocabulary from a directory tree (no Qt needed)
python3 exiftagger.py vocab ~/Photos --dry-run
python3 exiftagger.py vocab ~/Photos

# Synchronise the three caption tags across a tree (no Qt needed)
python3 exiftagger.py sync ~/Photos

# ...synchronise, keeping an <file>.orig backup of each photo written
python3 exiftagger.py sync ~/Photos --backup
```

The vocabulary is stored at `~/.local/share/exiftagger/vocabulary.json`.

Editor keyboard shortcuts: **Alt+A** caption, **Alt+U** decoding selector,
**Alt+I** include first 8 bytes, **Alt+K** keywords, **Alt+L** long description,
**Alt+O** OK, **Alt+C** / **Esc** cancel.

## License

Copyright (C) 2026 Niccolo Rigacci niccolo@rigacci.org

This program is free software: you can redistribute it and/or modify it under the
terms of the GNU General Public License as published by the Free Software
Foundation, either version 3 of the License, or (at your option) any later
version (GPL-3.0-or-later). See the LICENSE file for details.

Project page: https://github.com/RigacciOrg/exiftagger
