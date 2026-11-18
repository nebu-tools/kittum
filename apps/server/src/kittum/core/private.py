"""Recognising a private value (a key, a password, the contact address) in text or bytes.

`PrivateValues` is the one matcher the gateway's scrub, the recording checks, and the bundle's
seal scan all use. For each value it recognises exactly these spellings:

- each character of the value written in any of these ways, chosen independently per character:
  - literally (in bytes, its UTF-8 encoding);
  - as `%XX` for each byte of its UTF-8 encoding, with the hex digits in either case, whether or
    not that character needs encoding;
  - as a JSON `\\uXXXX` escape, hex digits in either case (a surrogate pair outside the BMP);
  - as a JSON backslash escape, for the characters that have one (`\\"`, `\\\\`, `\\/`, `\\b`,
    `\\f`, `\\n`, `\\r`, `\\t`);
  - as `+`, for a space;
- the base64 and the base64url form of the value's UTF-8 bytes, at each of the three alignments
  it can take inside a longer base64 text (only the characters that the value alone decides).

It recognises nothing else: not the value in another letter case, not split by line breaks, not
in other encodings or compressed. Each spelling has a bounded length and the pattern has no
nested repetition, so a search takes time linear in the size of what is searched.
"""

import base64
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

_JSON_ESCAPES = {'"': '\\"', "\\": "\\\\", "/": "\\/", "\b": "\\b", "\f": "\\f", "\n": "\\n"}
_JSON_ESCAPES |= {"\r": "\\r", "\t": "\\t"}


def _hex(byte: int) -> str:
    """A regex for two hex digits of `byte`, letters in either case."""
    return "".join(f"[{d.lower()}{d.upper()}]" if d.isalpha() else d for d in f"{byte:02x}")


def _character(char: str) -> str:
    """A regex for one character of a value in every per-character spelling."""
    forms = [re.escape(char), "".join("%" + _hex(b) for b in char.encode())]
    units = char.encode("utf-16-be")
    forms.append(
        "".join(r"\\u" + _hex(units[i]) + _hex(units[i + 1]) for i in range(0, len(units), 2))
    )
    if char in _JSON_ESCAPES:
        forms.append(re.escape(_JSON_ESCAPES[char]))
    if char == " ":
        forms.append(r"\+")
    return "(?:" + "|".join(forms) + ")"


def _base64_forms(value: str) -> list[str]:
    """The base64 and base64url characters that `value`'s bytes alone decide, at each alignment."""
    data = value.encode()
    found: list[str] = []
    for offset in range(3):
        encoded = base64.b64encode(b"\0" * offset + data).decode().rstrip("=")
        start = -(-offset * 8 // 6)
        end = (offset + len(data)) * 8 // 6
        part = encoded[start:end]
        if part:
            found += [part, part.replace("+", "-").replace("/", "_")]
    return found


def _pattern(value: str) -> str:
    spelled = "".join(_character(c) for c in value)
    forms = sorted(set(_base64_forms(value)), key=len, reverse=True)
    return "|".join([spelled, *(re.escape(f) for f in forms)])


class PrivateValues:
    """The private values of a deployment, recognised in each spelling the module names."""

    def __init__(self, values: Iterable[str | None]) -> None:
        # Longest first, so a value that holds another is matched whole.
        self.values: tuple[str, ...] = tuple(
            sorted(dict.fromkeys(v for v in values if v), key=len, reverse=True)
        )
        if not self.values:
            self._text = self._bytes = None
            return
        source = "|".join(f"(?P<v{i}>{_pattern(v)})" for i, v in enumerate(self.values))
        self._text = re.compile(source)
        self._bytes = re.compile(source.encode())

    def __bool__(self) -> bool:
        return bool(self.values)

    def find(self, data: str | bytes) -> str | None:
        """The first private value `data` holds, in any recognised spelling, or None."""
        if isinstance(data, str):
            found = self._text.search(data) if self._text else None
        else:
            found = self._bytes.search(data) if self._bytes else None
        return None if found is None else self._value(found)

    def holds(self, data: str | bytes) -> bool:
        return self.find(data) is not None

    def replace(self, text: str, stand_in: Callable[[str, bool], str]) -> str:
        """`text` with each recognised spelling of a value replaced by `stand_in(value, plain)`,
        where `plain` says whether it was written literally."""
        if self._text is None:
            return text
        return self._text.sub(lambda m: stand_in(self._value(m), m.group() == self._value(m)), text)

    def _value(self, found: re.Match[str] | re.Match[bytes]) -> str:
        assert found.lastgroup is not None
        return self.values[int(found.lastgroup[1:])]


__all__ = ["PrivateValues"]
