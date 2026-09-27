"""Literal input candidates sourced only from the trusted user objective."""

import re


def quoted_inputs(objective: str):
    # Quotes identify possible values, not commands or their target fields. The
    # policy still chooses the appropriate observed control and candidate.
    pattern = r'"([^"\n]{1,12000})"|“([^”\n]{1,12000})”|「([^」\n]{1,12000})」'
    seen = set()
    for match in re.finditer(pattern, objective):
        group = next(i for i in range(1, 4) if match.group(i) is not None)
        value = match.group(group)
        if value.strip() and value not in seen:
            seen.add(value)
            yield {"value": value, "start": match.start(group), "end": match.end(group)}
        if len(seen) >= 16:
            break
