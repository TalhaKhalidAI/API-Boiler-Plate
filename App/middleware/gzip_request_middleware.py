import gzip
import io
import json

from App.core.LoggingInit import get_core_logger
from App.core.settings import settings

logger = get_core_logger(__name__)

MAX_DECOMPRESSED_SIZE = settings.MAX_DECOMPRESSED_BODY_SIZE


async def _send_json(send, status_code: int, payload: dict):
    """Send a minimal JSON ASGI response."""
    body = json.dumps(payload).encode()
    await send({
        "type": "http.response.start",
        "status": status_code,
        "headers": [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode()),
        ],
    })
    await send({
        "type": "http.response.body",
        "body": body,
        "more_body": False,
    })


class GZipRequestMiddleware:
    """
    Pure ASGI middleware (NOT BaseHTTPMiddleware).

    Reason: BaseHTTPMiddleware.call_next uses the OUTER request's receive
    channel regardless of any new Request(...) we build or any
    request._receive override we do. Mutating the receive channel is only
    possible in a raw ASGI middleware.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        # ---- Look for Content-Encoding: gzip ----
        headers = {k.lower(): v for k, v in scope["headers"]}
        encoding = headers.get(b"content-encoding", b"").decode("latin-1").lower()

        if encoding != "gzip":
            return await self.app(scope, receive, send)

        # ---- Read the full request body from receive ----
        body = b""
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            if message["type"] != "http.request":
                continue
            body += message.get("body", b"")
            if not message.get("more_body", False):
                break

        # ---- Empty body: strip header, pass through ----
        if not body:
            scope["headers"] = [
                (k, v) for k, v in scope["headers"]
                if k.lower() != b"content-encoding"
            ]
            return await self.app(scope, receive, send)

        # ---- Decompress ----
        try:
            with gzip.GzipFile(fileobj=io.BytesIO(body)) as f:
                decompressed = f.read(MAX_DECOMPRESSED_SIZE + 1)
        except Exception as exc:
            logger.warning(f"Gzip decompression failed: {exc}")
            return await _send_json(
                send, 400,
                {"error": "invalid_gzip_body", "message": "Failed to decompress gzip body"},
            )

        if len(decompressed) > MAX_DECOMPRESSED_SIZE:
            logger.warning(
                f"Gzip decompressed body exceeds limit: "
                f"{len(decompressed)} > {MAX_DECOMPRESSED_SIZE} bytes"
            )
            return await _send_json(
                send, 413,
                {
                    "error": "request_too_large",
                    "message": f"Decompressed body exceeds {MAX_DECOMPRESSED_SIZE} bytes",
                },
            )

        # ---- Rewrite scope headers: drop content-encoding, fix content-length ----
        new_headers = [
            (k, v) for k, v in scope["headers"]
            if k.lower() not in (b"content-encoding", b"content-length")
        ]
        new_headers.append((b"content-length", str(len(decompressed)).encode()))
        scope["headers"] = new_headers

        # ---- New receive that yields the decompressed body exactly once ----
        sent = False

        async def new_receive():
            nonlocal sent
            if sent:
                # Subsequent calls (rare) return empty body.
                return {"type": "http.request", "body": b"", "more_body": False}
            sent = True
            return {"type": "http.request", "body": decompressed, "more_body": False}

        return await self.app(scope, new_receive, send)