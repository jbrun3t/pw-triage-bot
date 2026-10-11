"""The one representation every date in the store is kept in: naive UTC."""

from __future__ import annotations

import datetime

from pwlite import PwError


def stamp(value: str | None) -> datetime.datetime | None:
    """Convert one of patchwork's dates to a naive UTC datetime; a zone-carrying value is converted, not trusted."""
    if not value:
        return None
    try:
        parsed = datetime.datetime.fromisoformat(value)
    except ValueError as e:
        raise PwError(f"cannot read the date {value!r}: {e}") from e
    if parsed.tzinfo is None:
        return parsed
    return parsed.astimezone(datetime.UTC).replace(tzinfo=None)


def now() -> datetime.datetime:
    """Return the present, in the terms every date here is kept in."""
    return datetime.datetime.now(datetime.UTC).replace(tzinfo=None)
