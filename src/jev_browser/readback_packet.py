"""Current list evidence by row, with bounded pages and explicit coverage limits."""
from __future__ import annotations

import re

from .protocol import digest

PAGE_ROWS = 6


def list_packet(obs, transition, *, cursor=0):
    """Prioritize literal hints; matching is never a confirmation or a task parser.

    The original observation remains the quote corpus. Grid values have one
    row-scoped representation instead of body/control/cell duplicates. Pages
    share the same captured frame, and omit no rows silently.
    """
    hints = [(p.get("name", ""), " ".join(p.get("value", "").casefold().split()))
             for p in transition.get("field_snapshot", [])
             if len(p.get("value", "").strip()) >= 3 and p.get("value") != "[redacted]"]
    records = []
    grid_text = set()
    for grid in obs.grids:
        grid_text.add(grid.name.strip())
        for row in grid.rows:
            grid_text.add(str(row.key).strip())
            values = [" ".join(c.value.casefold().split()) for c in row.cells]
            matches = sorted({name for name, value in hints if any(
                re.search(r"(?<!\w)" + re.escape(value) + r"(?!\w)", v) for v in values)})
            lines = [f"Grid {grid.id}; row {row.key}; {c.column} = {c.value}" for c in row.cells]
            # EvidenceNote quotes have a 1,200-character limit. Keep exact
            # consecutive cell lines together without inventing a new quote.
            chunks, current, omitted = [], "", 0
            for line in lines:
                if len(line) > 1200:
                    if current:
                        chunks.append(current)
                        current = ""
                    omitted += 1
                    continue
                joined = current + "\n" + line if current else line
                if len(joined) > 1200:
                    chunks.append(current)
                    current = line
                else:
                    current = joined
            if current:
                chunks.append(current)
            records.append({"grid_ref": grid.id, "row_ref": row.key,
                            "literal_match_fields": matches, "quotes": chunks,
                            "oversized_cells_omitted": omitted})
            for cell in row.cells:
                grid_text.update(x.strip() for x in cell.value.splitlines())
                grid_text.add(cell.column.strip())
    records.sort(key=lambda r: -len(r["literal_match_fields"]))  # Stable original order for ties.
    if not isinstance(cursor, int) or isinstance(cursor, bool) or cursor < 0 or cursor % PAGE_ROWS:
        raise ValueError("invalid readback page cursor")
    if cursor and cursor >= len(records):
        raise ValueError("readback page cursor outside current observation")
    evidence = {}
    oversized_quotes = 0

    def add(quote):
        nonlocal oversized_quotes
        if not quote.strip() or len(quote) > 1200:
            oversized_quotes += int(len(quote) > 1200)
            return None
        ref = f"v{digest([obs.observation_id, quote])[:16]}"
        evidence[ref] = quote
        return ref

    # Retain chrome/status/error text, but table values are quoted with their
    # row context. Their standalone body strings cannot prove another row.
    omitted_body = 0
    for line in obs.text.splitlines():
        if line.strip() in grid_text:
            omitted_body += 1
        else:
            add(line)
    add(f"URL: {obs.url}")
    for e in obs.elements:
        if e.row_ref:
            continue  # The row's values are above; no row control grants a write.
        add(f"{e.role} {e.name} = {e.value}"
            + (f"; checked={str(e.checked).lower()}" if e.checked is not None else "")
            + (f"; menu_owner={e.menu_owner}" if e.menu_owner else "")
            + (f"; popup={e.popup_kind}; open={e.popup_open}" if e.popup_kind else ""))
    selected = records[cursor:cursor + PAGE_ROWS]
    rows = [{k: v for k, v in r.items() if k != "quotes"} | {
        "evidence_ids": [ref for q in r["quotes"] if (ref := add(q))]} for r in selected]
    next_cursor = cursor + len(selected) if cursor + len(selected) < len(records) else None
    return {
        "readback_evidence": evidence,
        "list_evidence": {
            "observation_id": obs.observation_id, "document_version": obs.document_version,
            "cursor": cursor, "next_cursor": next_cursor,
            "visible_rows": len(records), "rows_in_packet": len(rows),
            "rows_outside_packet": len(records) - len(rows),
            "literal_matching_rows": sum(bool(r["literal_match_fields"]) for r in records),
            "rows": rows, "table_body_lines_represented_by_rows": omitted_body,
            "row_controls_represented_by_cells": sum(bool(e.row_ref) for e in obs.elements),
            "oversized_quotes_omitted": oversized_quotes,
            "scope": "Captured visible rows only; not the whole database. Literal matches are "
                     "retrieval hints only, not identity or save confirmation. Request next_cursor "
                     "if more current rows are needed. Missing evidence never authorizes replay.",
        },
    }
