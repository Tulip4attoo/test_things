"""Publication-date windows shared by collection and export."""
from datetime import date
import re


def date_window(*, month=None, since=None, before=None):
    """Return inclusive start / exclusive end as ISO calendar dates."""
    if month:
        if since or before:
            raise ValueError("Use --month on its own, or use --since and/or --before")
        if not re.fullmatch(r"\d{4}-\d{2}", month):
            raise ValueError("Month must use YYYY-MM, for example 2026-10")
        try:
            year, number = map(int, month.split("-"))
            start = date(year, number, 1)
            end = date(year + (number == 12), 1 if number == 12 else number + 1, 1)
        except ValueError as exc:
            raise ValueError(f"Invalid month: {month}") from exc
        return start.isoformat(), end.isoformat()
    try:
        start = date.fromisoformat(since).isoformat() if since else None
        end = date.fromisoformat(before).isoformat() if before else None
    except ValueError as exc:
        raise ValueError("Dates must use YYYY-MM-DD") from exc
    if start and end and start >= end:
        raise ValueError("--since must be earlier than --before (exclusive)")
    return start, end
