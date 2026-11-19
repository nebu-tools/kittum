"""The seal scan reads inside SQLite files, zips, gzip, and the text streams of a PDF, within its
limits, and refuses a file it cannot read through."""

import base64
import gzip
import io
import sqlite3
import zipfile
import zlib
from typing import TYPE_CHECKING

import pymupdf
import pytest

from kittum.core import PrivateValues
from kittum.ledger.scan import MAX_DEPTH, MAX_PIECE, ScanRefused, holds_private
from kittum.report import AiInvolvement, PrismaFlow, SourceCount, prisma_svg

if TYPE_CHECKING:
    from pathlib import Path

ADDRESS = "librarian@example.org"
MATCHER = PrivateValues([ADDRESS, "db-password-marker-0b4e"])


def _holds(data: bytes) -> bool:
    return holds_private(data, MATCHER, "file")


def _zip(members: dict[str, bytes]) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    return out.getvalue()


def _flow(source_name: str) -> PrismaFlow:
    ai = AiInvolvement(**dict.fromkeys(AiInvolvement.model_fields, 0))
    counts = {n: 0 for n, f in PrismaFlow.model_fields.items() if f.annotation is int}
    return PrismaFlow(
        topic_id="TRIALS",
        scope_id="main",
        identity_revision_id=None,
        identified_by_source=(
            SourceCount(source="pubmed", name=source_name, kind="bibliographic", records=3),
        ),
        exclusion_reasons=(),
        ai_title_abstract=ai,
        ai_full_text=ai,
        members={},
        checks=(),
        **counts,
    )


def figure_pdf(source_name: str) -> bytes:
    """Kittum's PRISMA figure, as SVG from its renderer, turned into a PDF by MuPDF."""
    drawing = prisma_svg(_flow(source_name), "PRISMA 2020 flow").encode()
    with pymupdf.open(stream=drawing, filetype="svg") as document:
        return document.convert_to_pdf()


def test_a_value_in_the_text_of_a_pdf_figure_is_found() -> None:
    pdf = figure_pdf(f"PubMed, asked as {ADDRESS}")
    assert not MATCHER.holds(pdf)  # not in the file as it is: the text is compressed
    assert _holds(pdf)
    assert not _holds(figure_pdf("PubMed"))


def _lzw_encode(data: bytes) -> bytes:
    """LZW as PDF writes it (EarlyChange 1), clearing the table when it is full."""
    codes: list[tuple[int, int]] = []
    table = {bytes([i]): i for i in range(256)}
    width, word = 9, b""
    codes.append((256, width))
    for byte in data:
        grown = word + bytes([byte])
        if grown in table:
            word = grown
            continue
        codes.append((table[word], width))
        table[grown] = len(table) + 2
        # The reader adds each entry one code later than the writer, so the writer widens when
        # its next code reaches a power of two, one past where the reader does.
        if width == 12 and len(table) + 2 >= 4094:
            codes.append((256, width))
            table = {bytes([i]): i for i in range(256)}
            width = 9
        elif width < 12 and len(table) + 2 >= 1 << width:
            width += 1
        word = bytes([byte])
    if word:
        codes.append((table[word], width))
    codes.append((257, width))
    bits = "".join(format(code, f"0{w}b") for code, w in codes)
    bits += "0" * (-len(bits) % 8)
    return int(bits, 2).to_bytes(len(bits) // 8) if bits else b""


def _pdf_with_stream(raw: bytes, filters: str) -> bytes:
    """A one-page PDF with an extra stream object, unused by the page, holding `raw`."""
    with pymupdf.open() as document:
        document.new_page()
        xref = document.get_new_xref()
        document.update_object(xref, "<<>>")
        document.update_stream(xref, raw, new=True, compress=False)
        document.xref_set_key(xref, "Filter", filters)
        return document.tobytes()


def test_the_lzw_decoder_matches_mupdf() -> None:
    text = b"".join(b"record %d of the search, asked as someone; " % i for i in range(900))
    pdf = _pdf_with_stream(_lzw_encode(text), "/LZWDecode")
    with pymupdf.open(stream=pdf, filetype="pdf") as document:
        xref = document.xref_length() - 1
        assert document.xref_stream(xref) == text  # the encoder is right, as MuPDF reads it
    assert not _holds(pdf)
    planted = text[:20000] + ADDRESS.encode() + text[20000:]
    assert _holds(_pdf_with_stream(_lzw_encode(planted), "/LZWDecode"))


@pytest.mark.parametrize(
    ("encode", "filters"),
    [
        (zlib.compress, "/FlateDecode"),
        (lambda d: d.hex().encode() + b">", "/ASCIIHexDecode"),
        (lambda d: b"<~" + base64.a85encode(d) + b"~>", "/ASCII85Decode"),
        (_lzw_encode, "/LZWDecode"),
        (
            lambda d: zlib.compress(_lzw_encode(d).hex().encode() + b">"),
            "[/FlateDecode /ASCIIHexDecode /LZWDecode]",
        ),
    ],
    ids=["flate", "asciihex", "ascii85", "lzw", "chained"],
)
def test_a_value_in_a_pdf_stream_is_found_through_its_text_filters(
    encode: object, filters: str
) -> None:
    data = b"BT (sent by " + ADDRESS.encode() + b") Tj ET"
    pdf = _pdf_with_stream(encode(data), filters)  # type: ignore[operator]
    assert not MATCHER.holds(pdf)
    assert _holds(pdf)


def test_a_pdf_with_a_filter_the_scan_does_not_know_is_refused() -> None:
    with pytest.raises(ScanRefused, match="RunLengthDecode"):
        _holds(_pdf_with_stream(b"\x02abc\x80", "/RunLengthDecode"))


def test_a_pdf_that_needs_a_password_is_refused() -> None:
    with pymupdf.open() as document:
        document.new_page().insert_text((72, 72), "nothing private")
        locked = document.tobytes(
            encryption=pymupdf.PDF_ENCRYPT_AES_256, owner_pw="owner-pw-1234", user_pw="user-pw-1234"
        )
    with pytest.raises(ScanRefused, match="needs a password"):
        _holds(locked)


def test_a_value_in_a_zip_in_a_zip_is_found() -> None:
    inner = _zip({"sheet.xml": b"<c>" + ADDRESS.encode() + b"</c>"})
    outer = _zip({"readme.txt": b"nothing here", "nested/book.xlsx": inner})
    assert not MATCHER.holds(outer)
    assert _holds(outer)
    assert not _holds(_zip({"nested/book.xlsx": _zip({"sheet.xml": b"<c>1</c>"})}))


def test_a_value_in_gzip_is_found() -> None:
    assert _holds(gzip.compress(b"log line: " + ADDRESS.encode()))


def test_containers_nested_deeper_than_the_limit_are_refused() -> None:
    data = b"nothing here"
    for depth in range(MAX_DEPTH + 1):
        data = _zip({f"level{depth}.zip": data})
    with pytest.raises(ScanRefused, match="containers deep"):
        _holds(data)


def test_a_value_in_an_sqlite_row_on_overflow_pages_is_found(tmp_path: Path) -> None:
    path = tmp_path / "bundle.sqlite"
    with sqlite3.connect(path) as connection:
        connection.execute("create table note (id integer, body text, data blob)")
        long = "x" * 50_000 + ADDRESS + "y" * 50_000
        connection.execute("insert into note values (1, ?, null)", (long,))
    connection.close()
    data = path.read_bytes()
    # The row is split over overflow pages, so the value is not in the file in one piece.
    assert len(data) > 3 * 4096
    assert _holds(data)


def test_a_value_in_a_compressed_sqlite_blob_is_found(tmp_path: Path) -> None:
    path = tmp_path / "bundle.sqlite"
    with sqlite3.connect(path) as connection:
        connection.execute("create table artifact (id integer, data blob)")
        connection.execute("insert into artifact values (1, ?)", (gzip.compress(ADDRESS.encode()),))
    connection.close()
    assert _holds(path.read_bytes())


def _bomb(size: int) -> bytes:
    """gzip of `size` zero bytes, built a megabyte at a time."""
    engine = zlib.compressobj(9, zlib.DEFLATED, 31)
    chunk = bytes(1024 * 1024)
    out = [engine.compress(chunk) for _ in range(size // len(chunk))]
    return b"".join([*out, engine.flush()])


def test_an_inflate_bomb_is_refused() -> None:
    bomb = _bomb(MAX_PIECE + 1024 * 1024)
    assert len(bomb) < 1024 * 1024
    with pytest.raises(ScanRefused, match=f"decodes to more than {MAX_PIECE} bytes"):
        _holds(bomb)
    with pytest.raises(ScanRefused, match=f"decodes to more than {MAX_PIECE} bytes"):
        _holds(_zip({"bomb.gz": bomb}))
