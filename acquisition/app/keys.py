"""Musical key notation. The library standard is Camelot (8A = A minor, 8B = C major)."""
from __future__ import annotations

import re

_PITCH = {'C': 0, 'D': 2, 'E': 4, 'F': 5, 'G': 7, 'A': 9, 'B': 11}
_CAMELOT = re.compile(r'0?([1-9]|1[0-2])\s*([AB])', re.I)
# Traktor's Open Key: 1m = A minor (8A), 1d = C major (8B).
_OPEN_KEY = re.compile(r'0?([1-9]|1[0-2])\s*([MD])', re.I)
# The root letter must be uppercase so a genre such as "EBM" is not read as E-flat minor.
_STANDARD = re.compile(r'([A-G])\s*([#♯b♭]?)\s*((?i:m|min|minor|maj|major))?')


def camelot(value):
    """Return the Camelot key for a Camelot, Open Key or standard key, or None."""
    text = str(value or '').strip()
    if match := _CAMELOT.fullmatch(text):
        return match[1] + match[2].upper()
    if match := _OPEN_KEY.fullmatch(text):
        return str((int(match[1]) + 6) % 12 + 1) + ('A' if match[2].lower() == 'm' else 'B')
    match = _STANDARD.fullmatch(text)
    if not match:
        return None
    pitch = (_PITCH[match[1]] + {'#': 1, '♯': 1, 'b': -1, '♭': -1}.get(match[2], 0)) % 12
    minor = (match[3] or '').lower() in ('m', 'min', 'minor')
    if minor:
        return str(7 * (pitch - 8) % 12 + 1) + 'A'
    return str(7 * (pitch - 11) % 12 + 1) + 'B'


def key_fields(raw):
    """Library record fields: the Camelot key and, when it differs, the tag as written."""
    raw = str(raw or '').strip() or None
    key = camelot(raw)
    return {'key': key, 'key_tag': raw if raw and raw != key else None}


def sort_key(value):
    """Order Camelot keys around the wheel: 1A, 1B, 2A ..."""
    match = _CAMELOT.fullmatch(str(value or ''))
    return (int(match[1]), match[2].upper()) if match else (99, str(value or ''))
