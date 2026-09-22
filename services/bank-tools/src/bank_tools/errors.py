"""Tool-level errors returned as structured boundary responses."""
from __future__ import annotations


class ToolError(Exception):
    code = "TOOL_ERROR"

    def __init__(self, message: str, code: str | None = None):
        super().__init__(message)
        if code:
            self.code = code


class NotFoundError(ToolError):
    code = "NOT_FOUND"


class ConflictError(ToolError):
    code = "CONFLICT"


class InvalidTransitionError(ToolError):
    code = "INVALID_TRANSITION"


class ReviewAlreadyDecidedError(ToolError):
    """A review decision targets a HumanReviewRequest that is no longer PENDING (stale/duplicate)."""
    code = "REVIEW_ALREADY_DECIDED"


class LeaseLostError(Exception):
    """This worker's idempotency lease was expired and taken over (or released); it must not write the result."""
