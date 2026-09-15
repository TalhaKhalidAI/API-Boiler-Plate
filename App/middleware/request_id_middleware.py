# App/middleware/request_id_middleware.py
"""Attach a unique request ID to every request/response for tracing."""

import uuid
from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware


class RequestIDMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, header_name: str = "X-Request-ID"):
        super().__init__(app)
        self.header_name = header_name

    async def dispatch(self, request: Request, call_next):
        import re
        raw_id = request.headers.get(self.header_name, "")
        req_id = re.sub(r"[\r\n]", "", raw_id)[:64]
        if not req_id:
            req_id = str(uuid.uuid4())
        request.state.request_id = req_id

        response = await call_next(request)
        response.headers[self.header_name] = req_id
        return response