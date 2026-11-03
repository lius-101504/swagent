"""Typed errors that cross the MCP boundary.

Every failure carries a short machine code, a human message and, where we know
one, a concrete hint the agent can act on. The MCP layer turns these into
``isError`` tool results rather than protocol errors, so the model reading the
transcript sees the cause instead of a bare exception.
"""


class SwError(Exception):
    """A SolidWorks operation failed in a way the caller can understand."""

    def __init__(self, message, *, code="sw_error", hint=None, details=None):
        super().__init__(message)
        self.message = message
        self.code = code
        self.hint = hint
        self.details = details or {}

    def to_dict(self):
        payload = {"ok": False, "error": self.message, "code": self.code}
        if self.hint:
            payload["hint"] = self.hint
        if self.details:
            payload["details"] = self.details
        return payload

    def __str__(self):
        return self.message


class NotConnected(SwError):
    def __init__(self, message, **kw):
        hint = kw.pop("hint", None) or (
            "Start SolidWorks and sign in to a licence first, then call sw_status again."
        )
        super().__init__(message, code="not_connected", hint=hint, **kw)


class NoDocument(SwError):
    def __init__(self, message="No active SolidWorks document.", **kw):
        hint = kw.pop("hint", None) or "Call sw_new_part (or sw_open_part) before modelling."
        super().__init__(message, code="no_document", hint=hint, **kw)
