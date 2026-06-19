"""Project rules-engine flags into the review queue.

A standalone reconcile the nightly routine calls after run_rules — it never
touches the certified engine. Every open flag becomes one open kind='flag'
queue item keyed to the flag id; any flag-item whose flag has cleared is
auto-closed. Both statements are set-based and the caller owns the
transaction.
"""

from __future__ import annotations

from uuid import UUID

import psycopg


def project_flags(conn: psycopg.Connection, client_id: UUID) -> None:
    """Open a queue item per open flag; auto-close items for cleared flags."""
    # (a) upsert an open item for every open flag, keyed to the flag itself.
    # detail is a JSON string for engine flags but PLAIN TEXT for sync-written
    # ones (transform_warning, qbo_deleted); `IS JSON` nests the former
    # without double-encoding and wraps the latter as a string — so the cast
    # never crashes the projection on real flags. CASE short-circuits per row
    # (detail is a column, not a constant), so detail::jsonb runs only when valid.
    conn.execute(
        """
        INSERT INTO review_queue (client_id, kind, priority, source_type,
                                  source_ref, title, payload)
        SELECT client_id, 'flag', severity, 'flag', id::text, rule_code,
               jsonb_build_object(
                   'rule_code', rule_code, 'severity', severity,
                   'target_type', source_type, 'target_ref', source_ref,
                   'detail', CASE WHEN detail IS JSON THEN detail::jsonb
                                  ELSE to_jsonb(detail) END)
        FROM flags WHERE client_id = %s AND status = 'open'
        ON CONFLICT (client_id, source_type, source_ref) WHERE status = 'open'
        DO UPDATE SET priority = excluded.priority, title = excluded.title,
                      payload = excluded.payload
        """,
        (client_id,),
    )
    # (b) auto-close any open flag-item whose flag is no longer open.
    conn.execute(
        """
        UPDATE review_queue
        SET status = 'dismissed', resolved_at = now(), resolved_by = 'system',
            resolution_note = 'underlying flag resolved'
        WHERE client_id = %s AND kind = 'flag' AND status = 'open'
          AND NOT EXISTS (SELECT 1 FROM flags f
                          WHERE f.id::text = review_queue.source_ref
                            AND f.status = 'open')
        """,
        (client_id,),
    )
