"""Pub/Sub MFA — placeholder for the future trigger-based design.

The intended flow: an inbound email gets forwarded (Gmail forwarder + Zapier,
or a domain-level Pub/Sub push) to a Pub/Sub topic. This strategy pulls the
most recent matching message and extracts the MFA code.

Not implemented yet — the structure exists so the strategy can be selected
via env var (``MFA_STRATEGY=pubsub``) once the pipeline is wired up.
"""

from __future__ import annotations

from .base import MFAStrategy


class PubSubMFAStrategy(MFAStrategy):
    def __init__(self, *, project_id: str, subscription: str, code_regex: str) -> None:
        self._project_id = project_id
        self._subscription = subscription
        self._code_regex = code_regex

    async def fetch_code(self, *, since_epoch: float, timeout_seconds: int) -> str:
        raise NotImplementedError(
            "PubSubMFAStrategy is a stub. Implement once the email→Pub/Sub "
            "pipeline (e.g. Gmail forwarder + Zapier, or domain push) is in place."
        )
