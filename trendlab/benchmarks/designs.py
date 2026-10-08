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


# Harder set: each flaw is a second-order consequence of a reasonable-sounding decision.
HARD_DESIGNS = [
    {
        "id": "signup",
        "doc": """# Unique usernames
On signup the API runs `SELECT 1 FROM users WHERE username = $1`; if no row comes back it runs
`INSERT INTO users (...)`. The users table has an index on username for fast lookups. The API
runs on 8 instances behind a load balancer; marketing expects 50k signups in the first hour of
the launch email.""",
        "flaws": {
            "race: check-then-insert lets two concurrent signups take the same name (no unique constraint)": r"race|concurren|simultaneous|at the same time|unique (constraint|index)|both (insert|succeed)|toctou|check.then",
        },
    },
    {
        "id": "billing",
        "doc": """# Monthly billing run
A cron job runs at 00:05 server time (UTC) on the 1st of each month. For every active
subscription it charges `price` and sets `paid_until = paid_until + 30 days`. Customers are
global; invoices show the billing month in the customer's local time zone. Annual plans are
billed the same way with `price * 12`.""",
        "flaws": {
            "30-day increments drift from calendar months (billing date walks; Feb/31-day months)": r"30 days|drift|calendar month|february|31|month length|walk",
            "UTC run vs local-time invoice month: customers west of UTC see the previous month": r"time ?zone|utc|local time|wrong month|previous month|offset",
            "annual plans charged price*12 every month": r"annual.*(every|each) month|charged? (monthly|twelve|12 times)|annual.*monthly|12x|overcharg",
        },
    },
    {
        "id": "feed",
        "doc": """# Activity feed API
GET /feed?page=N returns 50 items with `ORDER BY created_at DESC OFFSET N*50 LIMIT 50`. New
activity is written constantly (about 200 items a minute at peak). The mobile app loads page 0,
then page 1 when the user scrolls. Each item shows the author's avatar; the handler loads the
author for each item with `get_user(item.author_id)`.""",
        "flaws": {
            "offset pagination with live inserts: items repeat or get skipped between pages": r"offset|skip|duplicat|repeat|shift|cursor|keyset|seek",
            "N+1 queries loading each author": r"n ?\+ ?1|per item|each item.*(query|call)|batch|join|50 (queries|calls)",
        },
    },
    {
        "id": "cache_stampede",
        "doc": """# Pricing cache
Prices come from a slow pricing service (2-3 s per call). We cache the full price list in Redis
with a 10-minute TTL. On a cache miss, the request handler calls the pricing service and writes
the result to Redis. The site serves 400 requests per second, most of which need prices.""",
        "flaws": {
            "stampede: when the TTL expires every concurrent request hits the slow service": r"stampede|thundering|dog.?pil|herd|concurrent (miss|request)|all (requests|at once)|single.?flight|lock",
        },
    },
]


def found(points: list[dict], pattern: str) -> bool:
    rx = re.compile(pattern, re.I)
    return any(rx.search(p.get("claim", "") + " " + p.get("why", "")) for p in points)
