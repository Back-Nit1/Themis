"""Policy thresholds, configurable through environment variables."""
import os

from .models import ClaimType


def _float_env(name: str, default: float) -> float:
    raw = os.environ.get(name)
    return float(raw) if raw else default


DEMO_AUTONOMOUS_CREDIT_LIMIT = _float_env("DEMO_AUTONOMOUS_CREDIT_LIMIT", 50.0)
DEMO_CREDIT_CONFIDENCE_THRESHOLD = _float_env("DEMO_CREDIT_CONFIDENCE_THRESHOLD", 0.8)

# A persisted customer-request evidence record older than this no longer authorizes a payment block
# (prevents replaying a stale/unrelated "please block this" statement from an earlier session).
CUSTOMER_REQUEST_EVIDENCE_MAX_AGE_SECONDS = _float_env("CUSTOMER_REQUEST_EVIDENCE_MAX_AGE_SECONDS", 86_400.0)

# Claim types eligible for autonomous provisional credit; anything else defers to human review.
PROVISIONAL_CREDIT_ELIGIBLE_CLAIM_TYPES = frozenset({
    ClaimType.UNAUTHORIZED_TRANSACTION,
    ClaimType.UNRECOGNIZED_MERCHANT,
    ClaimType.DUPLICATE_TRANSACTION,
    ClaimType.WRONG_AMOUNT,
})
