"""The seal scan: whether a bundle file holds a private value, inside compressed content too.

Every file is searched as it is, with `kittum.core.PrivateValues`. What it contains is searched
too, by the file's first bytes, whatever its name:

- an SQLite database: every value of every table, read through sqlite (text as text, a blob as
  content of its own), and the schema;
- a zip, and zip-based formats such as a spreadsheet: every member, inflated;
- gzip: the inflated content;
- a PDF: every object as PyMuPDF writes it out, every stream decoded through its text filters
  (FlateDecode, ASCIIHexDecode, ASCII85Decode, LZWDecode, chained in any order; Flate output is
  searched as inflated, without undoing a predictor), and the text of every page as PyMuPDF
  extracts it. A stream is decoded up to its first image filter (DCTDecode, JPXDecode,
  CCITTFaxDecode, JBIG2Decode), which is not decoded.

What a container holds is scanned in the same way, down to `MAX_DEPTH` containers deep. One
decoded piece may be at most `MAX_PIECE` bytes and all that one file decodes to at most
`MAX_TOTAL`. A file that goes over a limit, is nested deeper, or cannot be decoded where it
should be (a broken archive, an encrypted member, a filter the scan does not know) is refused:
`ScanRefused` names the file and the reason.
"""

import base64
import io
import re
import sqlite3
import zipfile
import zlib
from typing import TYPE_CHECKING

import pymupdf

if TYPE_CHECKING:
    from collections.abc import Callable

    from kittum.core import PrivateValues

MAX_DEPTH = 4
"""Containers inside containers the scan opens: a zip in a zip in a gzip is three."""
MAX_PIECE = 64 * 1024 * 1024
"""The most one decoded member, stream, or blob may inflate to."""
MAX_TOTAL = 512 * 1024 * 1024
"""The most all that one bundle file decodes to may add up to."""

_IMAGE_FILTERS = {"DCTDecode", "DCT", "JPXDecode", "CCITTFaxDecode", "CCF", "JBIG2Decode"}
_FILTER_NAME = re.compile(r"/([A-Za-z0-9]+)")


class ScanRefused(Exception):
    """A file the scan cannot vouch for; `reason` says why, `where` what part of the file."""

    def __init__(self, where: str, reason: str) -> None:
        super().__init__(f"{where}: {reason}")
        self.where = where
        self.reason = reason


def holds_private(data: bytes, matcher: PrivateValues, name: str) -> bool:
    """Whether `data`, the file `name`, holds a private value as it is or in what it contains.
    Raises `ScanRefused` for a file the scan cannot read through."""
    return _Scan(matcher).blob(data, 0, name)


class _Scan:
    def __init__(self, matcher: PrivateValues) -> None:
        self._matcher = matcher
        self._left = MAX_TOTAL

    def blob(self, data: bytes, depth: int, where: str) -> bool:
        if self._matcher.holds(data):
            return True
        opener = _kind(data, self)
        if opener is None:
            return False
        if depth >= MAX_DEPTH:
            raise ScanRefused(where, f"more than {MAX_DEPTH} containers deep")
        return opener(data, depth + 1, where)

    def spend(self, size: int, where: str) -> None:
        if size > MAX_PIECE:
            raise ScanRefused(where, f"decodes to more than {MAX_PIECE} bytes")
        self._left -= size
        if self._left < 0:
            raise ScanRefused(where, f"decodes to more than {MAX_TOTAL} bytes in all")

    def inflate(self, data: bytes, wbits: int, where: str) -> bytes:
        """`data` inflated, within the limits. A stream cut short gives what it holds."""
        out = bytearray()
        while data:
            engine = zlib.decompressobj(wbits)
            try:
                out += engine.decompress(data, MAX_PIECE + 1 - len(out))
            except zlib.error as error:
                raise ScanRefused(where, f"cannot be inflated ({error})") from None
            if len(out) > MAX_PIECE:
                self.spend(len(out), where)
            # gzip may hold several members; a zlib stream, only one.
            data = engine.unused_data if wbits == 31 and engine.eof else b""
        self.spend(len(out), where)
        return bytes(out)

    # -- containers --------------------------------------------------------------------------

    def zip(self, data: bytes, depth: int, where: str) -> bool:
        try:
            archive = zipfile.ZipFile(io.BytesIO(data))
            members = archive.infolist()
        except (zipfile.BadZipFile, ValueError) as error:
            raise ScanRefused(where, f"is not a readable zip ({error})") from None
        for member in members:
            inner = f"{where}!{member.filename}"
            if member.is_dir():
                continue
            if member.flag_bits & 0x1:
                raise ScanRefused(inner, "is encrypted")
            if member.file_size > MAX_PIECE:
                raise ScanRefused(inner, f"decodes to more than {MAX_PIECE} bytes")
            try:
                with archive.open(member) as stream:
                    content = stream.read(MAX_PIECE + 1)
            except (zipfile.BadZipFile, NotImplementedError, zlib.error, OSError) as error:
                raise ScanRefused(inner, f"cannot be inflated ({error})") from None
            self.spend(len(content), inner)
            if self._matcher.holds(member.filename) or self.blob(content, depth, inner):
                return True
        return False

    def gzip(self, data: bytes, depth: int, where: str) -> bool:
        return self.blob(self.inflate(data, 31, where), depth, where + "!gzip")

    def sqlite(self, data: bytes, depth: int, where: str) -> bool:
        connection = sqlite3.connect(":memory:")
        try:
            connection.deserialize(data)
            tables = connection.execute(
                "select name, coalesce(sql, '') from sqlite_master where type = 'table'"
            ).fetchall()
            for name, sql in tables:
                if self._matcher.holds(name) or self._matcher.holds(sql):
                    return True
                quoted = '"' + name.replace('"', '""') + '"'
                for row in connection.execute(f"select * from {quoted}"):
                    for value in row:
                        if isinstance(value, str) and self._matcher.holds(value):
                            return True
                        if isinstance(value, bytes) and self.blob(value, depth, f"{where}!{name}"):
                            return True
        except sqlite3.DatabaseError as error:
            raise ScanRefused(where, f"cannot be read as SQLite ({error})") from None
        finally:
            connection.close()
        return False

    def pdf(self, data: bytes, depth: int, where: str) -> bool:
        try:
            document = pymupdf.open(stream=data, filetype="pdf")
        except Exception as error:
            raise ScanRefused(where, f"cannot be read as a PDF ({error})") from None
        with document:
            if document.needs_pass:
                raise ScanRefused(where, "is a PDF that needs a password")
            for xref in range(1, document.xref_length()):
                inner = f"{where}!object {xref}"
                if self._matcher.holds(document.xref_object(xref, compressed=True)):
                    return True
                if not document.xref_is_stream(xref):
                    continue
                if self.blob(self._decoded(document, xref, inner), depth, inner):
                    return True
            for number in range(document.page_count):
                text = document[number].get_text()
                self.spend(len(text), f"{where}!page {number + 1}")
                if self._matcher.holds(text):
                    return True
        return False

    def _decoded(self, document: pymupdf.Document, xref: int, where: str) -> bytes:
        """The stream, decrypted, through its text filters, up to the first image filter."""
        kind, value = document.xref_get_key(xref, "Filter")
        names = _FILTER_NAME.findall(value) if kind in {"name", "array"} else []
        if kind not in {"null", "name", "array"} or (kind != "null" and not names):
            raise ScanRefused(where, f"has a filter the scan cannot read ({value})")
        data = document.xref_stream_raw(xref)
        for name in names:
            if name in _IMAGE_FILTERS:
                break
            decode = _FILTERS.get(name)
            if decode is None:
                raise ScanRefused(where, f"uses the filter {name}, which the scan cannot decode")
            data = decode(self, data, where)
        return data


def _kind(data: bytes, scan: _Scan) -> Callable[[bytes, int, str], bool] | None:
    if data[:4] in {b"PK\x03\x04", b"PK\x05\x06"}:
        return scan.zip
    if data[:2] == b"\x1f\x8b":
        return scan.gzip
    if data[:16] == b"SQLite format 3\x00":
        return scan.sqlite
    if b"%PDF-" in data[:1024]:
        return scan.pdf
    return None


# -- PDF filters -----------------------------------------------------------------------------


def _flate(scan: _Scan, data: bytes, where: str) -> bytes:
    return scan.inflate(data, 15, where)


def _ascii_hex(scan: _Scan, data: bytes, where: str) -> bytes:
    digits = re.sub(rb"\s", b"", data).split(b">", 1)[0]
    if len(digits) % 2:
        digits += b"0"
    try:
        decoded = bytes.fromhex(digits.decode("ascii"))
    except UnicodeDecodeError, ValueError:
        raise ScanRefused(where, "is not valid ASCIIHex") from None
    scan.spend(len(decoded), where)
    return decoded


def _ascii85(scan: _Scan, data: bytes, where: str) -> bytes:
    text = re.sub(rb"\s", b"", data)
    text = text.removeprefix(b"<~").split(b"~>", 1)[0]
    if text.count(b"z") * 4 > MAX_PIECE:
        raise ScanRefused(where, f"decodes to more than {MAX_PIECE} bytes")
    try:
        decoded = base64.a85decode(text)
    except ValueError:
        raise ScanRefused(where, "is not valid ASCII85") from None
    scan.spend(len(decoded), where)
    return decoded


def _lzw(scan: _Scan, data: bytes, where: str) -> bytes:
    """LZW as PDF writes it: codes of 9 to 12 bits, most significant bit first, the code width
    growing one code early (EarlyChange 1)."""
    clear, end = 256, 257
    fresh = [bytes([i]) for i in range(256)] + [b"", b""]
    table = list(fresh)
    out = bytearray()
    previous: bytes | None = None
    width, buffer, bits = 9, 0, 0
    for byte in data:
        buffer = (buffer << 8) | byte
        bits += 8
        while bits >= width:
            bits -= width
            code = buffer >> bits
            buffer &= (1 << bits) - 1
            if code == clear:
                table, width, previous = list(fresh), 9, None
                continue
            if code == end:
                scan.spend(len(out), where)
                return bytes(out)
            if code < len(table):
                entry = table[code]
            elif code == len(table) and previous is not None:
                entry = previous + previous[:1]
            else:
                raise ScanRefused(where, "is not valid LZW")
            out += entry
            if len(out) > MAX_PIECE:
                scan.spend(len(out), where)
            if previous is not None:
                table.append(previous + entry[:1])
            previous = entry
            if len(table) >= (1 << width) - 1 and width < 12:
                width += 1
    scan.spend(len(out), where)
    return bytes(out)


_FILTERS: dict[str, Callable[[_Scan, bytes, str], bytes]] = {
    "FlateDecode": _flate,
    "Fl": _flate,
    "ASCIIHexDecode": _ascii_hex,
    "AHx": _ascii_hex,
    "ASCII85Decode": _ascii85,
    "A85": _ascii85,
    "LZWDecode": _lzw,
    "LZW": _lzw,
}


__all__ = ["MAX_DEPTH", "MAX_PIECE", "MAX_TOTAL", "ScanRefused", "holds_private"]
