# ExifTagger

Tag and caption JPEG photos through their EXIF/XMP metadata. A PyQt6 editor for
one image: a short caption synced across EXIF UserComment, ImageDescription and
XMP dc:description; keyword chips (XMP dc:Subject) backed by a local vocabulary;
and a long accessibility description. Plus Qt-free CLI actions, usable over SSH.
Built on exiv2.

## Rationale

Photo archives rely on a handful of key metadata fields, but general-purpose
editors bury them under dozens of tags. ExifTagger focuses on just the three
things a cataloguer actually edits per image — a short caption, a keyword list,
and a long (accessibility) description — behind one small, fast window. It reads
and writes standard EXIF/XMP tags through `exiv2`, so the results stay
interoperable with other tools.

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
- **Keyboard-friendly**: Alt-key mnemonics for every field, arrow-key suggestion
  picking, Esc to cancel, and an unsaved-changes confirmation.
- **Command-line actions** that do not import Qt, so they run over SSH on a
  headless host: build the vocabulary from a photo tree, and synchronise the
  three caption tags.
- Leaving a field empty removes its tag on save.
- **Free and Open Source Software** (GPL-3.0-or-later): a single lightweight
  Python script with no heavy dependencies beyond Qt and exiv2.

## Requirements

- **Python 3** (3.8 or newer).
- **PyQt6** — needed only for the graphical editor; the CLI actions run without it.
- **exiv2** 0.28 — the external command used to read and write the metadata.

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

# Build the keyword vocabulary from a directory tree (no Qt needed)
python3 exiftagger.py vocab ~/Photos --dry-run
python3 exiftagger.py vocab ~/Photos

# Synchronise the three caption tags across a tree (no Qt needed)
python3 exiftagger.py sync ~/Photos
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
