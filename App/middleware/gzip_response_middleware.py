import gzip
from typing import Callable

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response, StreamingResponse
from App.core.settings import settings
from App.core.LoggingInit import get_core_logger

logger = get_core_logger(__name__)

DEFAULT_MINIMUM_SIZE = 500

EXCLUDED_MEDIA_TYPES = {
    "image/",
    "video/",
    "audio/",
    "application/zip",
    "application/gzip",
    "application/x-gzip",
    "application/octet-stream",
    "application/pdf",
}


class GZipResponseMiddleware(BaseHTTPMiddleware):
    """
    Compress outgoing response bodies with gzip.

    Activates only when:
      - Client sends: Accept-Encoding: gzip
      - Response body >= minimum_size
      - Response is not already encoded
      - Content-Type is not a binary format
      - Response is not a StreamingResponse (would buffer entire stream in RAM)
    """

    def __init__(self, app, minimum_size: int = DEFAULT_MINIMUM_SIZE, compresslevel: int = 5):
        super().__init__(app)
        self.minimum_size = minimum_size
        self.compresslevel = compresslevel

    async def dispatch(self, request: Request, call_next: Callable):
        response = await call_next(request)

        accept_encoding = request.headers.get("accept-encoding", "")
        if "gzip" not in accept_encoding.lower():
            return response

        if response.headers.get("content-encoding"):
            return response

        media_type = (response.media_type or "").lower()
        if any(media_type.startswith(excluded) for excluded in EXCLUDED_MEDIA_TYPES):
            return response

        # Streaming responses: never buffer into memory. Pass through as-is.
        if isinstance(response, StreamingResponse):
            return response

        chunks = []
        async for chunk in response.body_iterator:
            chunks.append(chunk)
        body = b"".join(chunks)

        if len(body) < self.minimum_size:
            return Response(
                content=body,
                status_code=response.status_code,
                headers=dict(response.headers),
                media_type=response.media_type,
                background=response.background,        # ← preserve background task
            )

        compressed = gzip.compress(body, compresslevel=self.compresslevel)

        headers = dict(response.headers)
        headers["content-encoding"] = "gzip"
        headers["vary"] = "accept-encoding"
        headers.pop("content-length", None)

        return Response(
            content=compressed,
            status_code=response.status_code,
            headers=headers,
            media_type=response.media_type,
            background=response.background,            # ← preserve background task
        )