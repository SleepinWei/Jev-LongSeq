"""Versioned compatibility fixes for upstream verifiers, never agent context."""
from __future__ import annotations

import hashlib
from pathlib import Path

BUSINESS_031_UPSTREAM_SHA256 = "492de1bd54adbc654daee1c00159884db99bc41fa7a28c420ae5a70422752df2"
BUSINESS_031_V11_UPSTREAM_SHA256 = "1c72c1319e3251f1c2b2a415715442e99ca4018770e47b400f67054646597189"
BUSINESS_031_PATCH = "business_031-bigcapital-schema-v1"


def verifier_source(task):
    """Return original/effective source and patch ID without changing the checkout.

    The published BigCapital image has DISPLAY_NAME and PUBLISHED_AT, but the
    pinned task queries CONTACT_NORMAL_NAME and a boolean PUBLISHED. Derive the
    same name comparison and boolean from the actual columns. All checks, point
    weights and thresholds stay upstream's. Refuse to patch an unaudited version.
    """
    original = Path(task["verify_py_path"]).read_text()
    if task["task_id"] != "business_031":
        return original, original, None
    source_hash = hashlib.sha256(original.encode()).hexdigest()
    # v1.1 fixes the schema queries upstream and strengthens the official checks.
    # Execute it verbatim; the legacy substitutions must never touch this oracle.
    if source_hash == BUSINESS_031_V11_UPSTREAM_SHA256:
        return original, original, None
    if source_hash != BUSINESS_031_UPSTREAM_SHA256:
        raise ValueError("business_031 verifier version changed; re-audit schema compatibility patch")
    effective = original.replace(
        "OR CONTACT_NORMAL_NAME = 'ananya reddy - ex employee' ",
        "OR LOWER(DISPLAY_NAME) = 'ananya reddy - ex employee' ",
    ).replace(
        "SELECT ID, DATE, DESCRIPTION, PUBLISHED FROM MANUAL_JOURNALS ",
        "SELECT ID, DATE, DESCRIPTION, (PUBLISHED_AT IS NOT NULL) AS PUBLISHED "
        "FROM MANUAL_JOURNALS ",
    )
    return original, effective, BUSINESS_031_PATCH
