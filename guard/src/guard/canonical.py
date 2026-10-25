"""Canonical JSON (RFC 8785, the JSON Canonicalization Scheme) and the hashes made from it.

Every hash the guard records (a policy, a request, a decision record) is the SHA-256 of a
value's canonical text, so the Python and TypeScript SDKs give the same hash for the same
value. The rules that differ from ``json.dumps``:

- object keys are sorted by their UTF-16 code units;
- numbers are written as ECMAScript writes them (:func:`number`): ``1e-7`` where Python
  writes ``1e-07``, ``100000000000000000000`` for ``1e20``, ``1e+21`` from there up, ``5``
  for ``5.0`` and ``0`` for ``-0.0``;
- an integer is written as the double it equals, so it must be one exactly (``2**53 + 1``
  is not; ``10**21`` is, and is written ``1e+21``), and NaN and infinity are refused, as
  RFC 8785 requires;
- strings are escaped as ``JSON.stringify`` does; a lone surrogate is refused.
"""

from __future__ import annotations

import hashlib
import math
from decimal import Decimal
from json.encoder import encode_basestring
from typing import Any

MAX_SAFE_INTEGER = 2**53 - 1


class CanonicalError(ValueError):
    """A value has no canonical form (NaN, a huge integer, a lone surrogate, a non-JSON type)."""


def number(value: int | float) -> str:
    """A number as ECMAScript's ``Number.prototype.toString`` writes it (RFC 8785, 3.2.2.3)."""
    if isinstance(value, int):
        if abs(value) <= MAX_SAFE_INTEGER:
            return str(value)
        try:
            exact = float(value) == value
        except OverflowError:
            exact = False
        if not exact:
            shown = str(value) if len(str(value)) <= 24 else f"{str(value)[:24]}..."
            msg = f"integer {shown} is not exactly a double, so it has no canonical form"
            raise CanonicalError(msg)
        value = float(value)
    if not math.isfinite(value):
        msg = f"{value} has no JSON form"
        raise CanonicalError(msg)
    if value == 0:
        return "0"
    sign = "-" if value < 0 else ""
    # repr gives the shortest digits that round-trip, which are ECMAScript's digits too.
    _, digits_tuple, exponent = Decimal(repr(abs(value))).as_tuple()
    digits = "".join(str(d) for d in digits_tuple).rstrip("0")
    assert isinstance(exponent, int)
    trailing = len(digits_tuple) - len(digits)
    k = len(digits)
    n = k + exponent + trailing  # value = 0.d1...dk * 10**n
    if k <= n <= 21:
        text = digits + "0" * (n - k)
    elif 0 < n <= 21:
        text = f"{digits[:n]}.{digits[n:]}"
    elif -6 < n <= 0:
        text = "0." + "0" * (-n) + digits
    else:
        e = n - 1
        mantissa = digits[0] + (f".{digits[1:]}" if k > 1 else "")
        text = f"{mantissa}e{'+' if e > 0 else '-'}{abs(e)}"
    return sign + text


def _string(value: str) -> str:
    if not value.isascii():
        try:
            value.encode("utf-8")
        except UnicodeEncodeError as error:
            msg = "a string holds a lone surrogate, which has no canonical form"
            raise CanonicalError(msg) from error
    text: str = encode_basestring(value)  # JSON.stringify's escapes, non-ASCII kept
    return text


def _write(value: Any, out: list[str]) -> None:
    if value is None:
        out.append("null")
    elif value is True:
        out.append("true")
    elif value is False:
        out.append("false")
    elif isinstance(value, str):
        out.append(_string(value))
    elif isinstance(value, int | float):
        out.append(number(value))
    elif isinstance(value, list | tuple):
        out.append("[")
        for index, item in enumerate(value):
            if index:
                out.append(",")
            _write(item, out)
        out.append("]")
    elif isinstance(value, dict):
        for key in value:
            if not isinstance(key, str):
                msg = f"object keys must be strings, got {key!r}"
                raise CanonicalError(msg)
        keys = sorted(value)  # UTF-16 code-unit order is code-point order for ASCII keys
        if not all(key.isascii() for key in keys):
            keys = sorted(value, key=lambda key: key.encode("utf-16-be", "surrogatepass"))
        out.append("{")
        for index, key in enumerate(keys):
            if index:
                out.append(",")
            out.append(_string(key))
            out.append(":")
            _write(value[key], out)
        out.append("}")
    else:
        msg = f"{type(value).__name__} is not a JSON value"
        raise CanonicalError(msg)


def canonical(value: Any) -> str:
    """The canonical JSON text of ``value`` (``dict``, ``list``, ``str``, numbers, bools, None)."""
    out: list[str] = []
    _write(value, out)
    return "".join(out)


def digest(value: Any) -> str:
    """The SHA-256 of ``value``'s canonical text, as 64 lowercase hex digits."""
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


__all__ = ["MAX_SAFE_INTEGER", "CanonicalError", "canonical", "digest", "number"]
