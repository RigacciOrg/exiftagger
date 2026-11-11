#!/usr/bin/env python3
"""exiftagger — view/edit short caption, keywords and long description of one JPEG.

Single-file deployment. Three entry points share the SAME Qt-free core:

    exiftagger.py IMAGE.jpg          # GUI editor (needs PyQt6)
    exiftagger.py vocab DIR [opts]   # build keyword vocabulary (no Qt, SSH-safe)
    exiftagger.py sync  DIR          # sync the 3 caption tags (no Qt, SSH-safe)

PyQt6 is imported ONLY inside gui_main(), so the CLI actions run on a headless
host without PyQt6 installed.

Metadata model
--------------
* caption  -> Exif.Photo.UserComment (UNICODE/Intel), Exif.Image.ImageDescription
              (UTF-8 as de-facto), Xmp.dc.description (UTF-8, x-default). Read with
              that precedence (first non-empty); written synchronised to all three.
* keywords -> Xmp.dc.subject (Bag, UTF-8).
* long     -> Xmp.iptc.ExtDescrAccessibility (UTF-8, x-default).

An empty field on save removes its tag(s). Target: Linux, exiv2 0.28.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import unicodedata
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from pathlib import Path

__author__ = "Niccolo Rigacci"
__copyright__ = "Copyright 2026 Niccolo Rigacci <niccolo@rigacci.org>"
__license__ = "GPLv3-or-later"
__email__ = "niccolo@rigacci.org"
__version__ = "0.1.1"

APP_NAME = "exiftagger"
EXIV2 = os.environ.get("EXIV2_COMMAND", "exiv2")


def exiv2_available():
    """True if the exiv2 command can be found (honours $EXIV2_COMMAND, which may
    be a bare name on PATH or an absolute path)."""
    return shutil.which(EXIV2) is not None

# Exit codes (useful when spawned from a picture browser):
EXIT_SAVED = 0          # OK pressed and something changed -> written
EXIT_ERROR = 1          # exiv2 / I/O error
EXIT_USAGE = 2          # bad arguments / file not found
EXIT_NOSAVE = 10        # editor closed without writing (Cancel or no change)


# ---------------------------------------------------------------------------
# Text sanitation
# ---------------------------------------------------------------------------
# Allowed: any BMP character, plus LF and TAB. Silently stripped: everything
# outside the BMP (emoji, ...), C0/C1 controls and NUL, U+FFFD, the BOM U+FEFF.

def clean_text(s):
    """Filter a free-text value. Keeps internal layout (newlines, tabs, runs of
    spaces); only outer trimming is applied by the caller where wanted."""
    out = []
    for ch in s:
        if ch == "\n" or ch == "\t":
            out.append(ch)
            continue
        o = ord(ch)
        if o > 0xFFFF:                      # outside the Basic Multilingual Plane
            continue
        if ch == "\ufffd" or ch == "\ufeff":
            continue
        if unicodedata.category(ch) == "Cc":  # C0/C1 controls incl. NUL
            continue
        out.append(ch)
    return "".join(out)


def collapse_spaces(s):
    """Trim ends and collapse internal runs of ASCII spaces to one."""
    return re.sub(r" {2,}", " ", s.strip())


def clean_keyword(s):
    """A keyword: no newlines/tabs, space-normalised, NFC."""
    s = clean_text(s).replace("\n", " ").replace("\t", " ")
    return unicodedata.normalize("NFC", collapse_spaces(s))


def clean_field(s):
    """A multi-line text field: sanitised, outer-trimmed, internal layout kept,
    NFC-normalised."""
    return unicodedata.normalize("NFC", clean_text(s).strip())


# ---------------------------------------------------------------------------
# exiv2 read helpers
# ---------------------------------------------------------------------------

def exiv2_run(args, path):
    """Run exiv2 with byte argv (locale-independent). Returns (rc, out, err)
    with out/err as bytes."""
    argv = [os.fsencode(EXIV2)] + [a if isinstance(a, bytes) else a.encode("utf-8")
                                   for a in args] + [os.fsencode(path)]
    try:
        r = subprocess.run(argv, capture_output=True)
    except FileNotFoundError:
        return 127, b"", b"exiv2 command not found"
    return r.returncode, r.stdout, r.stderr


def read_exif_value(path, key):
    """`exiv2 -K <key> -PEv` -> plain (vanilla) value as str, or None if absent."""
    rc, out, _ = exiv2_run(["-K", key, "-PEv"], path)
    if rc != 0:
        return None
    text = out.decode("utf-8", "replace").rstrip("\n")
    return text if text else None


def read_usercomment_hex(path):
    """`exiv2 -K Exif.Photo.UserComment -PEh` parsed into raw bytes, or None."""
    rc, out, _ = exiv2_run(["-K", "Exif.Photo.UserComment", "-PEh"], path)
    if rc != 0:
        return None
    return parse_hexdump(out.decode("latin-1"))


def parse_hexdump(text):
    """Parse the exiv2 -PEh dump: '  OFFSET  xx xx ... xx  ascii' per line."""
    data = bytearray()
    for line in text.splitlines():
        m = re.match(r"\s*[0-9A-Fa-f]{4}\s+(.*)", line)
        if not m:
            continue
        hexpart = m.group(1)[:48]           # 16 bytes * 3 chars = fixed 48-wide field
        for tok in hexpart.split():
            if len(tok) == 2:
                try:
                    data.append(int(tok, 16))
                except ValueError:
                    pass
    return bytes(data)


# XMP namespaces used in the packet produced by `exiv2 -pX`.
_NS = {
    "rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
    "dc": "http://purl.org/dc/elements/1.1/",
    "ipc": "http://iptc.org/std/Iptc4xmpCore/1.0/xmlns/",
    "xml": "http://www.w3.org/XML/1998/namespace",   # permanently bound to xml:
}


def read_xmp(path):
    """`exiv2 -pX` -> dict(dc_description, ext_descr, subjects list)."""
    rc, out, _ = exiv2_run(["-pX"], path)
    result = {"dc_description": "", "ext_descr": "", "subjects": []}
    if rc != 0 or not out.strip():
        return result
    try:
        root = ET.fromstring(out)
    except ET.ParseError:
        return result

    def alt_default(el):
        """x-default (or first) rdf:li under an rdf:Alt."""
        alt = el.find("rdf:Alt", _NS)
        if alt is None:
            return ""
        lis = alt.findall("rdf:li", _NS)
        lang_attr = "{%s}lang" % _NS["xml"]
        xd = [li for li in lis if li.get(lang_attr) == "x-default"]
        chosen = xd[0] if xd else (lis[0] if lis else None)
        return (chosen.text or "") if chosen is not None else ""

    for desc in root.iter("{%s}Description" % _NS["rdf"]):
        d = desc.find("dc:description", _NS)
        if d is not None:
            result["dc_description"] = alt_default(d)
        e = desc.find("ipc:ExtDescrAccessibility", _NS)
        if e is not None:
            result["ext_descr"] = alt_default(e)
        s = desc.find("dc:subject", _NS)
        if s is not None:
            bag = s.find("rdf:Bag", _NS)
            if bag is not None:
                result["subjects"] = [(li.text or "") for li in
                                      bag.findall("rdf:li", _NS)]
    return result


# ---------------------------------------------------------------------------
# UserComment decoding (EXIF 8-byte character-code prefix + anomalies)
# ---------------------------------------------------------------------------

PFX_ASCII = b"ASCII\x00\x00\x00"
PFX_JIS = b"JIS\x00\x00\x00\x00\x00"
PFX_UNICODE = b"UNICODE\x00"
PFX_UNDEFINED = b"\x00" * 8

# Alternative decodings offered in the combo (label, python codec).
ALT_ENCODINGS = [
    ("Unicode UTF-16 LE (Intel)", "utf-16-le"),
    ("Unicode UTF-16 BE (Motorola)", "utf-16-be"),
    ("ASCII", "ascii"),
    ("UTF-8", "utf-8"),
    ("JIS (ISO-2022-JP)", "iso-2022-jp"),
    ("Latin-1 (ISO-8859-1)", "iso-8859-1"),
    ("Windows-1252", "cp1252"),
    ("ISO-8859-15", "iso-8859-15"),
    ("Mac Roman", "mac-roman"),
]


def _is_ascii7(b):
    return all(x < 0x80 for x in b)


def _strict_decodes(b, codec):
    try:
        b.decode(codec)
        return True
    except (UnicodeDecodeError, LookupError):
        return False


def decode_usercomment(raw):
    """Decode UserComment honouring the EXIF prefix; return a dict:
        text        -> initial text to show (sanitised)
        anomalies   -> list[str] of reasons to flag (red border)
        include8    -> whether the first 8 bytes should be treated as content
        auto_codec  -> codec key to pre-select in the combo
    """
    anomalies = []
    # Absent or empty UserComment is the normal "no caption" state (cameras ship
    # it empty, and we write it empty too), not an error: show an empty box with
    # no warning. The red flag is reserved for the malformed cases below. Note a
    # present-but-empty undefined field (8 zero bytes) also reaches the
    # PFX_UNDEFINED branch and stays unflagged.
    if raw is None or len(raw) == 0:
        return {"text": "", "anomalies": [], "include8": False,
                "auto_codec": None}

    if len(raw) >= 1000:
        anomalies.append("Field of 1000 bytes or more.")

    prefix, payload = raw[:8], raw[8:]

    if prefix == PFX_UNICODE:
        codec = "utf-16-le"                 # default Intel unless a BOM says otherwise
        if payload[:2] == b"\xff\xfe":
            codec, payload = "utf-16-le", payload[2:]
        elif payload[:2] == b"\xfe\xff":
            codec, payload = "utf-16-be", payload[2:]
        if not _strict_decodes(payload, codec):
            anomalies.append("Unicode encoding declared, but invalid bytes.")
        text = payload.decode(codec, "replace")
        return {"text": clean_text(text), "anomalies": anomalies,
                "include8": False, "auto_codec": codec}

    if prefix == PFX_ASCII:
        if not _strict_decodes(payload, "ascii"):
            anomalies.append("ASCII encoding declared, but invalid bytes.")
        return {"text": clean_text(payload.decode("ascii", "replace")),
                "anomalies": anomalies, "include8": False, "auto_codec": "ascii"}

    if prefix == PFX_JIS:
        if not _strict_decodes(payload, "iso-2022-jp"):
            anomalies.append("JIS encoding declared, but invalid bytes.")
        return {"text": clean_text(payload.decode("iso-2022-jp", "replace")),
                "anomalies": anomalies, "include8": False,
                "auto_codec": "iso-2022-jp"}

    if prefix == PFX_UNDEFINED:
        # Charset unspecified: acceptable only if ASCII-7.
        if not _is_ascii7(payload):
            anomalies.append("Encoding not specified and content is not ASCII-7.")
        codec = auto_hint(payload)
        return {"text": clean_text(payload.decode(codec, "replace")),
                "anomalies": anomalies, "include8": False, "auto_codec": codec}

    # Unknown / invalid prefix: treat the WHOLE field as content, flag, and turn
    # on "include first 8 bytes".
    anomalies.append("Invalid EXIF prefix: the whole field is treated as text.")
    codec = auto_hint(raw)
    return {"text": clean_text(raw.decode(codec, "replace")),
            "anomalies": anomalies, "include8": True, "auto_codec": codec}


def auto_hint(b):
    """Pick an initial codec without external libraries. Order:
    BOM+alternating-NUL (UTF-16), strict UTF-8, else Windows-1252."""
    if b[:2] == b"\xff\xfe":
        return "utf-16-le"
    if b[:2] == b"\xfe\xff":
        return "utf-16-be"
    # alternating NUL heuristic for BOM-less UTF-16
    if len(b) >= 4:
        if b[1::2].count(0) > len(b) // 4 and b[0] != 0:
            return "utf-16-le"
        if b[0::2].count(0) > len(b) // 4 and b[1] != 0:
            return "utf-16-be"
    if _strict_decodes(b, "utf-8"):
        return "utf-8"
    return "cp1252"


def alt_decodings(raw, include8):
    """Decode `raw` with every alternative codec; each result sanitised for the
    UserComment field. Returns list[(label, codec, text)]."""
    if raw is None:
        return []
    data = raw if include8 else raw[8:]
    out = []
    for label, codec in ALT_ENCODINGS:
        try:
            text = data.decode(codec, "replace")
        except LookupError:
            continue
        out.append((label, codec, clean_text(text)))
    return out


# ---------------------------------------------------------------------------
# Caption read precedence
# ---------------------------------------------------------------------------

def read_caption(path):
    """First non-empty of UserComment -> ImageDescription -> dc.description."""
    raw = read_usercomment_hex(path)
    dec = decode_usercomment(raw)
    if dec["text"].strip():
        return dec["text"]
    img = read_exif_value(path, "Exif.Image.ImageDescription")
    if img and img.strip():
        return clean_field(img)
    xmp = read_xmp(path)
    if xmp["dc_description"].strip():
        return clean_field(xmp["dc_description"])
    return ""


# ---------------------------------------------------------------------------
# Vocabulary (JSON at $XDG_DATA_HOME/exiftagger/vocabulary.json)
# ---------------------------------------------------------------------------

def vocab_path():
    base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return Path(base) / APP_NAME / "vocabulary.json"


def identity_key(s):
    """NFC + space-reduce + casefold."""
    return collapse_spaces(unicodedata.normalize("NFC", s)).casefold()


def search_key(s):
    """NFKD + strip combining marks + space-reduce + casefold (accent-insensitive)."""
    d = unicodedata.normalize("NFKD", s)
    d = "".join(c for c in d if not unicodedata.combining(c))
    return collapse_spaces(d).casefold()


class Vocabulary:
    def __init__(self, keywords=None):
        self.display = []                    # list of display forms (NFC)
        self._by_identity = {}               # identity_key -> display
        for k in (keywords or []):
            self.add(k)

    def add(self, display):
        display = unicodedata.normalize("NFC", collapse_spaces(display))
        if not display:
            return None
        ik = identity_key(display)
        if ik not in self._by_identity:
            self._by_identity[ik] = display
            self.display.append(display)
        return self._by_identity[ik]

    def resolve(self, typed):
        """Return the vocabulary display form if `typed` matches by identity,
        else the cleaned typed form."""
        ik = identity_key(typed)
        return self._by_identity.get(ik, clean_keyword(typed))

    def suggest(self, query, limit=12):
        """Accent- and case-insensitive substring match, capped."""
        q = search_key(query)
        if len(q) < 3:
            return []
        hits = [d for d in self.display if q in search_key(d)]
        hits.sort(key=lambda d: (search_key(d).find(q), d.casefold()))
        return hits[:limit]

    def to_json(self):
        return {"version": 1,
                "keywords": sorted(self.display, key=lambda s: s.casefold())}

    @classmethod
    def load(cls, path=None):
        path = Path(path) if path else vocab_path()
        if not path.is_file():
            return cls()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return cls()
        return cls(data.get("keywords", []))

    def save(self, path=None):
        path = Path(path) if path else vocab_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(self.to_json(), ensure_ascii=False, indent=2,
                       sort_keys=False) + "\n",
            encoding="utf-8")


# ---------------------------------------------------------------------------
# exiv2 write
# ---------------------------------------------------------------------------

def quoted(value):
    """Wrap in double quotes. exiv2 strips exactly the outer pair of a command
    value, so wrapping protects inner quotes/spaces/commas. (Confirmed on 0.28.)"""
    return '"' + value + '"'


def build_write_commands(caption, keywords, ext_descr):
    """exiv2 -M commands. Empty field -> its tag(s) removed. One invocation."""
    cmds = []
    caption = caption.strip()
    if caption:
        # UTF-16 byte order is left to exiv2: it follows the file's Exif/TIFF
        # container (II -> Intel/LE, MM -> Motorola/BE), keeping UserComment
        # consistent with the rest of the Exif block. II is the common case, so
        # this yields the spec's "default Intel"; a rare MM source would get BE.
        cmds.append(f"set Exif.Photo.UserComment Comment charset=Unicode {caption}")
        cmds.append(f"set Exif.Image.ImageDescription Ascii {quoted(caption)}")
        cmds.append(f"set Xmp.dc.description LangAlt lang=x-default {caption}")
    else:
        # Empty caption -> the normal "no caption" state, close to a camera's:
        # UserComment present but *undefined* (just the 8 zero bytes, no padding),
        # ImageDescription present but empty (a single zero byte), and
        # dc:description removed (cameras never add it). These exact command forms
        # were verified against exiv2 0.28; both re-read as empty with no warning.
        cmds += ["set Exif.Photo.UserComment charset=Undefined",
                 "set Exif.Image.ImageDescription",
                 "del Xmp.dc.description"]

    # Reset the Bag first: 'set' on an existing Bag appends. If no keywords, only
    # the 'del' remains -> tag removed.
    cmds.append("del Xmp.dc.subject")
    cmds += [f"set Xmp.dc.subject XmpBag {quoted(k)}" for k in keywords if k]

    ext_descr = ext_descr.strip()
    if ext_descr:
        cmds.append(
            f"set Xmp.iptc.ExtDescrAccessibility LangAlt lang=x-default {ext_descr}")
    else:
        cmds.append("del Xmp.iptc.ExtDescrAccessibility")
    return cmds


def make_original_backup(path):
    """Create <path>.orig ONCE, as a byte-for-byte copy of the untouched file.
    Written only on the first run (if it does not already exist), so later runs
    preserve the true pre-exiv2 original rather than overwriting it.
    Returns (ok, error_text)."""
    orig = Path(str(path) + ".orig")
    if orig.exists():
        return True, ""
    try:
        shutil.copy2(path, orig)
        return True, ""
    except OSError as e:
        return False, f"could not create backup {orig}: {e}"


def write_metadata(path, caption, keywords, ext_descr, backup=True):
    """Write in place. With backup=True, a write-once <path>.orig copy is made
    first; if that fails the write is aborted. Returns (ok, stderr_text)."""
    if backup:
        ok, err = make_original_backup(path)
        if not ok:
            return False, err
    cmds = build_write_commands(caption, keywords, ext_descr)
    argv = [os.fsencode(EXIV2), b"-n", b"UTF-8"]
    for c in cmds:
        argv += [b"-M", c.encode("utf-8")]
    argv.append(os.fsencode(path))
    try:
        r = subprocess.run(argv, capture_output=True)
    except FileNotFoundError:
        return False, f"exiv2 command not found: {EXIV2}"
    return r.returncode == 0, r.stderr.decode("utf-8", "replace").strip()


# ---------------------------------------------------------------------------
# CLI: filesystem walk
# ---------------------------------------------------------------------------

def iter_jpegs(root):
    root = Path(root)
    for p in sorted(root.rglob("*")):
        if p.is_file() and p.suffix.lower() in (".jpg", ".jpeg"):
            yield p


# ---------------------------------------------------------------------------
# CLI action: build vocabulary
# ---------------------------------------------------------------------------

def cli_build_vocab(args):
    root = Path(args.directory)
    if not root.is_dir():
        print(f"Directory not found: {root}", file=sys.stderr)
        return EXIT_USAGE

    # Group variants accent/case-insensitively; within a group pick the most
    # frequent display form, tie-break on most uppercase + accents.
    groups = defaultdict(Counter)
    scanned = 0
    for jpg in iter_jpegs(root):
        scanned += 1
        for subj in read_xmp(jpg)["subjects"]:
            disp = unicodedata.normalize("NFC", collapse_spaces(subj))
            if disp:
                groups[search_key(disp)][disp] += 1

    chosen = []
    for key, counter in groups.items():
        best = max(counter.items(),
                   key=lambda kv: (kv[1], _upper_accent_score(kv[0])))
        chosen.append(best[0])

    path = Path(args.vocab) if args.vocab else vocab_path()
    if args.mode == "merge":
        vocab = Vocabulary.load(path)
    else:
        vocab = Vocabulary()
    added = []
    for k in chosen:
        before = len(vocab.display)
        vocab.add(k)
        if len(vocab.display) > before:
            added.append(k)

    print(f"Photos scanned: {scanned}; distinct keywords found: {len(chosen)}; "
          f"new in vocabulary: {len(added)}")
    if args.dry_run:
        for k in sorted(added, key=str.casefold):
            print("  +", k)
        print(f"(dry-run: {path} was not written)")
        return EXIT_SAVED
    vocab.save(path)
    print(f"Vocabulary written: {path} ({len(vocab.display)} keywords)")
    return EXIT_SAVED


def _upper_accent_score(s):
    uppers = sum(1 for c in s if c.isupper())
    accents = sum(1 for c in unicodedata.normalize("NFD", s)
                  if unicodedata.combining(c))
    return uppers + accents


# ---------------------------------------------------------------------------
# CLI action: sync the three caption tags
# ---------------------------------------------------------------------------

def cli_sync(args):
    root = Path(args.directory)
    if not root.is_dir():
        print(f"Directory not found: {root}", file=sys.stderr)
        return EXIT_USAGE
    errors = 0
    for jpg in iter_jpegs(root):
        caption = read_caption(jpg)
        if not caption:
            print(f"  (empty)  {jpg}")
            continue
        keywords = [unicodedata.normalize("NFC", collapse_spaces(s))
                    for s in read_xmp(jpg)["subjects"]]
        ext = clean_field(read_xmp(jpg)["ext_descr"])
        ok, err = write_metadata(jpg, caption, keywords, ext,
                                 backup=not args.no_backup)
        if ok:
            print(f"  sync     {jpg}")
        else:
            errors += 1
            print(f"  ERROR    {jpg}: {err}", file=sys.stderr)
    return EXIT_ERROR if errors else EXIT_SAVED


# ---------------------------------------------------------------------------
# GUI (PyQt6 imported here only)
# ---------------------------------------------------------------------------

def gui_main(image_path, backup=True):
    from PyQt6.QtCore import Qt, QPoint, QEvent
    from PyQt6.QtGui import QGuiApplication
    from PyQt6.QtWidgets import (
        QApplication, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QTextEdit,
        QLineEdit, QComboBox, QCheckBox, QPushButton, QListWidget, QFrame,
        QSizePolicy, QLayout, QMessageBox,
    )

    def tagged_label(text, tip):
        """A short label whose tooltip carries the exiv2 key(s) it maps to."""
        lbl = QLabel(text)
        lbl.setToolTip(tip)
        return lbl

    # ---- QFlowLayout: wraps chips to new rows (not shipped with Qt) ----------
    class QFlowLayout(QLayout):
        def __init__(self, margin=0, spacing=6):
            super().__init__()
            self._items = []
            self.setContentsMargins(margin, margin, margin, margin)
            self.setSpacing(spacing)

        def addItem(self, item):
            self._items.append(item)

        def count(self):
            return len(self._items)

        def itemAt(self, i):
            return self._items[i] if 0 <= i < len(self._items) else None

        def takeAt(self, i):
            return self._items.pop(i) if 0 <= i < len(self._items) else None

        def expandingDirections(self):
            return Qt.Orientation(0)

        def hasHeightForWidth(self):
            return True

        def heightForWidth(self, width):
            return self._do_layout(0, 0, width, apply=False)

        def setGeometry(self, rect):
            super().setGeometry(rect)
            self._do_layout(rect.x(), rect.y(), rect.width(), apply=True)

        def sizeHint(self):
            return self.minimumSize()

        def minimumSize(self):
            from PyQt6.QtCore import QSize
            s = QSize()
            for it in self._items:
                s = s.expandedTo(it.minimumSize())
            m = self.contentsMargins()
            return s + QSize(m.left() + m.right(), m.top() + m.bottom())

        def _do_layout(self, x0, y0, width, apply):
            x, y, line_h = x0, y0, 0
            space = self.spacing()
            for it in self._items:
                w, h = it.sizeHint().width(), it.sizeHint().height()
                if x + w > x0 + width and line_h > 0:
                    x = x0
                    y += line_h + space
                    line_h = 0
                if apply:
                    from PyQt6.QtCore import QRect
                    it.setGeometry(QRect(QPoint(x, y), it.sizeHint()))
                x += w + space
                line_h = max(line_h, h)
            return y + line_h - y0

    # ---- a single removable chip --------------------------------------------
    class Chip(QFrame):
        def __init__(self, text, on_remove):
            super().__init__()
            self.text = text
            self.setStyleSheet(
                "QFrame{background:#e8eefc;border:1px solid #c7d6f5;"
                "border-radius:11px;} QLabel{border:0;}")
            lay = QHBoxLayout(self)
            lay.setContentsMargins(10, 3, 6, 3)
            lay.setSpacing(6)
            lay.addWidget(QLabel(text))
            x = QPushButton("\u2715")
            x.setFixedSize(16, 16)
            x.setCursor(Qt.CursorShape.PointingHandCursor)
            x.setStyleSheet("QPushButton{border:0;background:transparent;}")
            x.clicked.connect(lambda: on_remove(self))
            lay.addWidget(x)

    # ---- keyword editor: one line + wrapping chips + suggestion popup --------
    class KeywordEditor(QWidget):
        def __init__(self, vocab, initial):
            super().__init__()
            self.vocab = vocab
            self._keys = []                  # ordered display forms (identity-unique)
            v = QVBoxLayout(self)
            v.setContentsMargins(0, 0, 0, 0)
            kw_label = QLabel("&Keywords (Xmp.dc.subject)")
            v.addWidget(kw_label)
            self.edit = QLineEdit(placeholderText="Type at least 3 characters…")
            self.edit.setStyleSheet("QLineEdit{background:#ffffff;}")
            kw_label.setBuddy(self.edit)               # Alt+K
            self.edit.textEdited.connect(self._on_typed)
            self.edit.returnPressed.connect(self._commit_typed)
            v.addWidget(self.edit)
            v.addWidget(QLabel("Assigned to this photo"))
            self.chip_host = QWidget()
            self.chip_flow = QFlowLayout()
            self.chip_host.setLayout(self.chip_flow)
            # Reserve room for ~3 rows of chips; this area never collapses.
            self.chip_host.setMinimumHeight(3 * 30 + 2 * 6)
            self.chip_host.setSizePolicy(QSizePolicy.Policy.Preferred,
                                         QSizePolicy.Policy.Minimum)
            v.addWidget(self.chip_host)
            # The editor takes only the height it needs (incl. the 3-chip-row
            # minimum); it does not stretch, so the long-description box sits
            # directly beneath it.
            self.setSizePolicy(QSizePolicy.Policy.Preferred,
                               QSizePolicy.Policy.Minimum)

            self.popup = QListWidget(self)
            self.popup.setWindowFlags(Qt.WindowType.ToolTip)
            self.popup.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            # Keep the highlighted row visible even though the list never holds
            # focus (it is driven from the text field by the arrow keys).
            self.popup.setStyleSheet(
                "QListWidget::item:selected{background:#cfe0fb;color:#000;}")
            self.popup.itemClicked.connect(
                lambda it: self._add(self.vocab.resolve(it.text())) or self._reset())
            self.edit.installEventFilter(self)

            for k in initial:
                self._add(k)

        def eventFilter(self, obj, event):
            if obj is self.edit:
                et = event.type()
                # Drive the suggestion list from the keyboard while the text
                # field keeps focus: Down/Up move the highlight, Enter picks the
                # highlighted item (else the typed text is committed), Escape
                # dismisses the list.
                if et == QEvent.Type.KeyPress and self.popup.isVisible():
                    key = event.key()
                    if key == Qt.Key.Key_Down:
                        self._move_selection(1)
                        return True
                    if key == Qt.Key.Key_Up:
                        self._move_selection(-1)
                        return True
                    if key == Qt.Key.Key_Escape:
                        self.popup.hide()
                        return True
                    if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                        it = self.popup.currentItem()
                        if it is not None and it.isSelected():
                            self._add(self.vocab.resolve(it.text()))
                            self._reset()
                            return True
                        # no highlighted suggestion -> let returnPressed commit
                # Dismiss the popup when the field truly loses focus. The two
                # reasons below are transient blips from clicking the popup
                # window itself, so they are ignored (preserves click-to-select).
                if (et == QEvent.Type.FocusOut
                        and event.reason() not in (Qt.FocusReason.PopupFocusReason,
                                                   Qt.FocusReason.ActiveWindowFocusReason)):
                    self.popup.hide()
            return False

        def _move_selection(self, delta):
            n = self.popup.count()
            if n == 0:
                return
            row = self.popup.currentRow()
            if row < 0:
                row = 0 if delta > 0 else n - 1
            else:
                row = max(0, min(n - 1, row + delta))
            self.popup.setCurrentRow(row)

        # keyword list API
        def keywords(self):
            return list(self._keys)

        def _add(self, display):
            display = self.vocab.resolve(display)
            if not display:
                return
            if any(identity_key(display) == identity_key(k) for k in self._keys):
                return
            self._keys.append(display)
            self.chip_flow.addWidget(Chip(display, self._remove))

        def _remove(self, chip):
            self._keys = [k for k in self._keys
                          if identity_key(k) != identity_key(chip.text)]
            chip.setParent(None)
            chip.deleteLater()

        def _commit_typed(self):
            text = self.edit.text().strip()
            if text:
                self._add(text)
            self._reset()

        def _reset(self):
            self.edit.clear()
            self.popup.hide()

        def _on_typed(self, text):
            matches = self.vocab.suggest(text)
            if not matches:
                self.popup.hide()
                return
            self.popup.clear()
            self.popup.addItems(matches)
            self.popup.setMinimumWidth(self.edit.width())
            self.popup.move(self.edit.mapToGlobal(QPoint(0, self.edit.height())))
            self.popup.setFixedHeight(min(len(matches), 10) * 22 + 4)
            self.popup.show()

    # ---- main window ---------------------------------------------------------
    class MainWindow(QWidget):
        CAPTION_NORMAL = "QTextEdit{background:#ffffff;}"
        CAPTION_ANOMALY = "QTextEdit{background:#ffffff;border:2px solid #b00020;}"

        def __init__(self, path, backup=True):
            super().__init__()
            self.path = Path(path)
            self.backup = backup
            self.saved = False
            self._closing = False       # set once accept()/reject() decided the outcome
            self.setWindowTitle(f"{APP_NAME} — {self.path.name}")

            scr = QGuiApplication.primaryScreen().availableGeometry()
            # 40% wide; height trimmed by two text lines, which comes off the
            # long-description box (the flexible region that fills leftover space).
            self.resize(int(scr.width() * 0.40),
                        int(scr.height() * 0.65) - 2 * 26)
            # Center on the available screen area (excludes panels; handles offset).
            frame = self.frameGeometry()
            frame.moveCenter(scr.center())
            self.move(frame.topLeft())

            # read current state
            self.raw = read_usercomment_hex(self.path)
            dec = decode_usercomment(self.raw)
            xmp = read_xmp(self.path)
            caption = (dec["text"].strip()
                       and dec["text"]) or read_caption(self.path)
            self._initial = {
                "caption": caption,
                "keywords": [unicodedata.normalize("NFC", collapse_spaces(s))
                             for s in xmp["subjects"]],
                "ext": clean_field(xmp["ext_descr"]),
            }
            self.vocab = Vocabulary.load()

            root = QVBoxLayout(self)

            # caption
            cap_label = tagged_label(
                "C&aption (Exif.Photo.UserComment)",
                "Exif.Photo.UserComment · Exif.Image.ImageDescription · "
                "Xmp.dc.description\n(read in this order; written to all three)")
            root.addWidget(cap_label)
            self.caption = QTextEdit()
            self.caption.setFixedHeight(3 * 26)
            self.caption.setStyleSheet(self.CAPTION_NORMAL)
            self.caption.setPlainText(caption)
            self.caption.textChanged.connect(self._clear_anomaly)
            root.addWidget(self.caption)
            cap_label.setBuddy(self.caption)            # Alt+A

            row = QHBoxLayout()
            self.combo = QComboBox()
            self.combo.currentIndexChanged.connect(self._apply_decoding)
            uc_label = tagged_label("&UserComment decoding:",
                                    "Exif.Photo.UserComment")
            uc_label.setBuddy(self.combo)              # Alt+U
            row.addWidget(uc_label)
            row.addWidget(self.combo, 1)
            self.inc8 = QCheckBox("&Include first 8 bytes")   # Alt+I toggles
            self.inc8.setToolTip("Include the EXIF 8-byte charset prefix in the "
                                 "decoded text")
            self.inc8.stateChanged.connect(self._on_inc8_toggled)
            row.addWidget(self.inc8)
            root.addLayout(row)

            self.warn = QLabel("")
            self.warn.setStyleSheet("color:#b00020;")
            self.warn.setWordWrap(True)
            root.addWidget(self.warn)

            # keywords
            self.kw = KeywordEditor(self.vocab, self._initial["keywords"])
            root.addWidget(self.kw)

            # long description
            ld_label = QLabel("&Long description (Xmp.iptc.ExtDescrAccessibility)")
            root.addWidget(ld_label)
            self.ext = QTextEdit()
            # Flexible region: absorbs extra height, and is the first to shrink
            # when the window gets shorter, so the chip area is never hidden.
            self.ext.setMinimumHeight(2 * 26)
            self.ext.setSizePolicy(QSizePolicy.Policy.Expanding,
                                   QSizePolicy.Policy.Expanding)
            self.ext.setStyleSheet("QTextEdit{background:#ffffff;}")
            self.ext.setPlainText(self._initial["ext"])
            root.addWidget(self.ext, 1)
            ld_label.setBuddy(self.ext)                # Alt+L

            # buttons
            btns = QHBoxLayout()
            btns.addStretch(1)
            cancel = QPushButton("&Cancel")            # Alt+C
            cancel.clicked.connect(self.reject)
            ok = QPushButton("&OK")                    # Alt+O
            ok.setDefault(True)
            ok.clicked.connect(self.accept)
            btns.addWidget(cancel)
            btns.addWidget(ok)
            root.addLayout(btns)

            # anomalies + combo
            self.inc8.blockSignals(True)
            self.inc8.setChecked(dec["include8"])
            self.inc8.blockSignals(False)
            self._rebuild_combo(select=dec["auto_codec"])
            if dec["anomalies"]:
                self._show_anomaly(" ".join(dec["anomalies"]))

        # anomaly marker on the caption box (white background in both states)
        def _show_anomaly(self, msg):
            self.caption.setStyleSheet(self.CAPTION_ANOMALY)
            self.warn.setText("\u26a0 " + msg)

        def _clear_anomaly(self):
            self.caption.setStyleSheet(self.CAPTION_NORMAL)
            self.warn.setText("")

        def _on_inc8_toggled(self, *_):
            # Keep the chosen codec, re-decode over the new byte range, and
            # reflect it in the caption at once (as a combo change would).
            self._rebuild_combo(select=self.combo.currentData(), apply_text=True)

        def _rebuild_combo(self, select=None, apply_text=False):
            self._decodings = alt_decodings(self.raw, self.inc8.isChecked())
            self.combo.blockSignals(True)
            self.combo.clear()
            idx = 0
            for i, (label, codec, _text) in enumerate(self._decodings):
                self.combo.addItem(label, codec)
                if select and codec == select:
                    idx = i
            self.combo.setCurrentIndex(idx)
            self.combo.blockSignals(False)
            if apply_text:
                self._apply_decoding(idx)

        def _apply_decoding(self, i):
            if 0 <= i < len(self._decodings):
                self.caption.setPlainText(self._decodings[i][2])
                self._clear_anomaly()

        # dirty check shared by accept / reject / closeEvent
        def _is_dirty(self):
            return (clean_field(self.caption.toPlainText())
                    != clean_field(self._initial["caption"])
                    or self.kw.keywords() != self._initial["keywords"]
                    or clean_field(self.ext.toPlainText()) != self._initial["ext"])

        def _confirm_discard(self):
            """True if the user agrees to discard unsaved changes."""
            r = QMessageBox.question(
                self, "Unsaved changes",
                "You have unsaved changes. Discard them?",
                QMessageBox.StandardButton.Discard
                | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel)
            return r == QMessageBox.StandardButton.Discard

        # dialog result
        def accept(self):
            caption = clean_field(self.caption.toPlainText())
            keywords = self.kw.keywords()
            ext = clean_field(self.ext.toPlainText())

            if not self._is_dirty():
                self.saved = False
                self._closing = True
                self.close()
                return
            ok, err = write_metadata(self.path, caption, keywords, ext,
                                     backup=self.backup)
            if not ok:
                self._show_anomaly("exiv2 write failed: " + err)
                return
            # vocabulary write-back: every keyword used becomes known
            for k in keywords:
                self.vocab.add(k)
            try:
                self.vocab.save()
            except OSError:
                pass
            self.saved = True
            self._closing = True
            self.close()

        def reject(self):
            if self._is_dirty() and not self._confirm_discard():
                return                          # stay in the editor
            self.saved = False
            self._closing = True
            self.close()

        def closeEvent(self, event):
            # accept()/reject() set _closing once the outcome is decided; only a
            # bare window-manager close (title-bar X, Alt+F4) still needs the
            # unsaved-changes guard here.
            if not self._closing:
                if self._is_dirty() and not self._confirm_discard():
                    event.ignore()
                    return
                self.saved = False
            event.accept()

        def keyPressEvent(self, event):
            # Esc acts as Cancel (QWidget has no built-in dialog behaviour). When
            # the keyword suggestion popup is open, its own filter consumes Esc
            # first, so a first Esc closes the popup and a second one cancels.
            if event.key() == Qt.Key.Key_Escape:
                self.reject()
                return
            super().keyPressEvent(event)

    app = QApplication(sys.argv[:1])
    win = MainWindow(image_path, backup=backup)
    win.show()
    app.exec()
    return EXIT_SAVED if win.saved else EXIT_NOSAVE


# ---------------------------------------------------------------------------
# Argument dispatch
# ---------------------------------------------------------------------------

def build_parser():
    p = argparse.ArgumentParser(prog=APP_NAME, description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("-V", "--version", action="version",
                   version=f"%(prog)s {__version__}")
    sub = p.add_subparsers(dest="command")

    pv = sub.add_parser("vocab",
                        help="build the keyword vocabulary from photos (Xmp.dc.subject)")
    pv.add_argument("directory")
    pv.add_argument("--dry-run", action="store_true")
    pv.add_argument("--mode", choices=["merge", "rebuild"], default="merge")
    pv.add_argument("--vocab", help="alternative vocabulary path")

    ps = sub.add_parser(
        "sync",
        help="sync Exif.Photo.UserComment / Exif.Image.ImageDescription / "
             "Xmp.dc.description")
    ps.add_argument("directory")
    ps.add_argument("--no-backup", action="store_true",
                    help="do not create the write-once <file>.orig backup")

    pe = sub.add_parser("edit", help="open the graphical editor on a JPEG")
    pe.add_argument("image")
    pe.add_argument("--no-backup", action="store_true",
                    help="do not create the write-once <file>.orig backup")
    return p


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    # Bare `exiftagger.py IMAGE.jpg` -> GUI, without needing the 'edit' subcommand.
    if argv and argv[0] not in ("vocab", "sync", "edit",
                                "-h", "--help", "-V", "--version"):
        argv = ["edit"] + argv

    args = build_parser().parse_args(argv)

    # Every command relies on the external exiv2; fail early with a clear message
    # rather than producing empty reads or a mid-run error.
    if args.command in ("vocab", "sync", "edit") and not exiv2_available():
        print(f"exiv2 command not found: {EXIV2}\n"
              "Install exiv2 (e.g. 'sudo apt install exiv2'), or set "
              "$EXIV2_COMMAND to its path.", file=sys.stderr)
        return EXIT_ERROR

    if args.command == "vocab":
        return cli_build_vocab(args)
    if args.command == "sync":
        return cli_sync(args)
    if args.command == "edit":
        src = Path(args.image)
        if not src.is_file():
            print(f"File not found: {src}", file=sys.stderr)
            return EXIT_USAGE
        try:
            return gui_main(args.image, backup=not args.no_backup)
        except ImportError as e:
            print(f"PyQt6 not available: {e}\n"
                  "The 'vocab' and 'sync' actions work without PyQt6.",
                  file=sys.stderr)
            return EXIT_ERROR

    build_parser().print_help()
    return EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main())
