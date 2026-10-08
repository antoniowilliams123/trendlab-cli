"""Secret exfiltration guard (uplift U11).

AUTO mode lets the agent use the network without asking. That is fine until the run has touched
a secret: a ``.env``, a private key, a credentials file. From then on (or when one command does
both) a network call needs approval in every mode, because it could carry the secret out.
Unattended runs deny approvals, so there it is refused.
"""

from __future__ import annotations

import re

SECRET_PATH = re.compile(
    r"(^|[/\s'\"=@<])("
    r"\.env(\.[\w.-]+)?|[\w.-]*\.pem|[\w.-]*\.key|id_(rsa|ed25519|ecdsa|dsa)|"
    r"credentials(\.json)?|\.netrc|\.npmrc|\.pypirc|\.pgpass|secrets?\.(json|ya?ml|toml)"
    r")(?=$|[\s'\";|&>)])"
    r"|\.ssh/|\.aws/|\.gnupg/|\.config/gcloud",
    re.I,
)
# .env.example / .env.sample / .env.template hold no secrets
_TEMPLATE = re.compile(r"\.env\.(example|sample|template|dist)\b", re.I)


def touches_secret(paths: list[str] | None = None, command: str | None = None) -> list[str]:
    """The secret-bearing paths named by these file paths or this command."""
    found = []
    for text in [*(paths or []), command or ""]:
        if not text:
            continue
        for m in SECRET_PATH.finditer(text):
            hit = m.group(0).strip(" '\"=@</")
            if hit and not _TEMPLATE.search(text[m.start() : m.end() + 12]):
                found.append(hit)
    return sorted(set(found))
