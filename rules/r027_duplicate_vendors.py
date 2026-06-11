"""R027 — vendor records that are probably the same vendor.

WHY: "Home Depot" and "Home Depot #4821" split one vendor's history in
two. That breaks 1099 totals (under-reporting risk), halves the baseline
R010/R020 key on, and lets duplicate invoices hide across the pair. Info
severity: the fix is a QBO merge, done at leisure.

Finding identity: the pair's LESSER entity uuid anchors the finding
(stable, resolvable source_ref); ALL similar partners of that anchor are
aggregated into one finding, so re-runs never duplicate and a vendor with
two near-twins yields one queue item, not three.
"""

from __future__ import annotations

from datetime import date
from uuid import UUID

import psycopg

from rules.base import Finding

rule_code: str = "R027"
severity: str = "info"
title: str = "Probable duplicate vendor names"
description: str = (
    "Pairs of ACTIVE vendors whose names are >= 0.7 trigram-similar — "
    "split history breaks 1099s and every vendor-keyed rule."
)

SIMILARITY: float = 0.7


def run(conn: psycopg.Connection, client_id: UUID, as_of: date) -> list[Finding]:
    rows = conn.execute(
        """
        SELECT anchor.id, anchor.name,
               array_agg(partner.name ORDER BY partner.id) AS partner_names,
               array_agg(partner.id::text ORDER BY partner.id) AS partner_ids,
               array_agg(round(similarity(anchor.name, partner.name)::numeric, 2)
                         ORDER BY partner.id) AS similarities
        FROM entities anchor
        JOIN entities partner
          ON partner.client_id = anchor.client_id
         AND anchor.id < partner.id
         AND partner.kind = 'vendor'
         AND partner.active
         AND partner.qbo_deleted_at IS NULL
         AND similarity(anchor.name, partner.name) >= %(threshold)s
        WHERE anchor.client_id = %(client_id)s
          AND anchor.kind = 'vendor'
          AND anchor.active
          AND anchor.qbo_deleted_at IS NULL
        GROUP BY anchor.id
        ORDER BY anchor.id
        """,
        {"client_id": client_id, "threshold": SIMILARITY},
    ).fetchall()
    return [
        Finding(
            source_type="entity",
            source_ref=str(anchor_id),
            detail={
                "vendor": anchor_name,
                "similar_to": [
                    {"name": name, "entity_id": partner_id,
                     "similarity": str(sim)}
                    for name, partner_id, sim in
                    zip(partner_names, partner_ids, similarities, strict=True)
                ],
            },
        )
        for anchor_id, anchor_name, partner_names, partner_ids, similarities
        in rows
    ]
