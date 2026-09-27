"""
Attach the API key to the request, once, before any view runs.

Resolving here rather than in each view means the token is looked up a single
time per request and the result is available to views, the audit trail and
logging without any of them re-parsing the header.
"""

from __future__ import annotations

import logging

from .auth_api import InvalidKey, authenticate_request

log = logging.getLogger("board")


class ApiKeyMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.api_key = None
        try:
            request.api_key = authenticate_request(request)
        except InvalidKey as exc:
            # Do not log the token. The reason alone is enough to diagnose.
            log.info("rejected api key on %s: %s", request.path, exc)
            request.api_key_error = str(exc)
        return self.get_response(request)
