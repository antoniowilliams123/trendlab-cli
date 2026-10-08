"""Design documents with planted flaws, for the critique-panel eval (U15).

Each design reads as plausible; each hides two flaws a careful reviewer should raise. A flaw
counts as found when any point's text matches one of its keyword patterns.
"""

from __future__ import annotations

import re

DESIGNS = [
    {
        "id": "webhook",
        "doc": """# Payment webhook handler
Stripe sends `charge.succeeded` events to POST /webhooks/stripe. The handler parses the JSON
body, looks up the order by `metadata.order_id`, marks it paid, and sends the confirmation email
and ships the goods by enqueueing a fulfilment job. The endpoint URL contains a random token
(/webhooks/stripe/8f3a...) so only Stripe knows it. Stripe retries a delivery when our endpoint
does not answer 2xx within 10 seconds; fulfilment can take up to 30 seconds, so the handler
returns 200 after fulfilment finishes. Errors are logged and the handler returns 500.""",
        "flaws": {
            "no idempotency: retried or duplicate deliveries fulfil twice": r"idempot|duplicate|twice|more than once|retr(y|ies).*(again|double|repeat)|replay",
            "no signature verification: a secret URL is not authentication": r"signature|verif|hmac|authenticat|spoof|forg|secret (url|token).*(leak|not)",
        },
    },
    {
        "id": "cache",
        "doc": """# Product cache
Product pages read from Postgres through a new in-process cache: a Python dict keyed by product
id, filled on first read. Admin edits write to Postgres directly through the admin service,
which runs as a separate process. The shop runs 6 web workers behind a load balancer. The
catalogue has 2 million products and grows 5% a month. The cache is never cleared except on
deploy, which happens weekly.""",
        "flaws": {
            "stale reads: admin writes never invalidate the per-worker caches": r"stale|invalidat|out of date|outdated|inconsisten|coheren",
            "unbounded memory: no eviction for 2M growing products in 6 workers": r"unbounded|memory|evict|lru|ttl|size limit|oom|grow",
        },
    },
    {
        "id": "jobs",
        "doc": """# Email job queue
Jobs are pushed to a Redis list. Workers BRPOP a job, render the template, call the email
provider, and on any exception push the job back to the head of the list so it is retried
immediately. Delivery is at-least-once. The `send_invoice` job charges the stored card for any
outstanding balance before emailing the invoice, so the email always shows a zero balance.""",
        "flaws": {
            "poison messages: a job that always fails is retried forever with no backoff": r"poison|forever|infinite|endless|dead.?letter|backoff|retry limit|max(imum)? retr",
            "non-idempotent side effect under at-least-once: cards charged twice": r"idempot|charge[sd]? (twice|again|multiple)|double.?charg|duplicate|exactly.?once",
        },
    },
    {
        "id": "upload",
        "doc": """# Avatar upload
POST /avatar accepts multipart form data. The server reads the whole file into memory, checks
the extension is .png or .jpg, and saves it as `/srv/static/avatars/<original filename>` so the
CDN can serve it at the same name. Users can upload a new avatar at any time; the newest file
wins.""",
        "flaws": {
            "path traversal / overwrite via the client-supplied filename": r"travers|\.\./|sanitiz|filename.*(user|client|attacker|overwrit)|overwrit",
            "no size limit: whole file in memory enables memory exhaustion": r"size limit|max(imum)? size|large file|memory|dos|denial|exhaust",
        },
    },
]


def found(points: list[dict], pattern: str) -> bool:
    rx = re.compile(pattern, re.I)
    return any(rx.search(p.get("claim", "") + " " + p.get("why", "")) for p in points)
