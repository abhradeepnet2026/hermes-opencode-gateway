"""OpenAI-compatible error responses.

All gateway errors are returned as JSON with this shape:

    {
      "error": {
        "message": "...",
        "type": "...",
        "param": null,
        "code": "..."
      }
    }

HTTP status codes follow OpenAI's conventions:
  400 → invalid_request_error   (bad body, unknown model field, etc.)
  401 → authentication_error
  404 → not_found                (unknown model when explicitly checked)
  429 → rate_limit_exceeded
  500 → internal_error           (opencode CLI failed)
  502 → bad gateway              (upstream provider error)
  504 → timeout                  (opencode run timed out)
"""
from __future__ import annotations

from typing import Any, Dict, Optional


class GatewayError(Exception):
    """Base class for all errors that map to a structured JSON response."""

    status_code: int = 500
    err_type: str = "internal_error"
    code: Optional[str] = None

    def __init__(self, message: str, *, code: Optional[str] = None, param: Optional[str] = None):
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
        self.param = param

    def to_payload(self) -> Dict[str, Any]:
        return {
            "error": {
                "message": self.message,
                "type": self.err_type,
                "param": self.param,
                "code": self.code,
            }
        }


class BadRequestError(GatewayError):
    status_code = 400
    err_type = "invalid_request_error"


class AuthenticationError(GatewayError):
    status_code = 401
    err_type = "authentication_error"


class RateLimitError(GatewayError):
    status_code = 429
    err_type = "rate_limit_exceeded"


class NotFoundError(GatewayError):
    status_code = 404
    err_type = "not_found"


class InternalError(GatewayError):
    status_code = 500
    err_type = "internal_error"


class UpstreamError(GatewayError):
    """opencode CLI ran but returned an error response."""
    status_code = 502
    err_type = "upstream_error"


class TimeoutError(GatewayError):
    status_code = 504
    err_type = "timeout"
