# App/middleware/idempotency_middleware.py
from fastapi import Request
from fastapi.responses import JSONResponse, Response
from starlette.middleware.base import BaseHTTPMiddleware
from App.store import idempotency_store
from App.core.LoggingInit import get_core_logger
import json

logger = get_core_logger(__name__)

class IdempotencyMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        # Only apply to mutating methods
        if request.method not in ["POST", "PUT", "PATCH"]:
            return await call_next(request)

        idem_key = request.headers.get("Idempotency-Key")
        if not idem_key:
            return await call_next(request)

        # 1. Check Redis for existing state
        state = await idempotency_store.get_status(idem_key)

        if state == "processing":
            # Another request is currently processing this key
            return JSONResponse(
                status_code=409,
                content={"error": "request_in_progress", "message": "A request with this Idempotency-Key is currently processing."}
            )

        if state:
            # We have a cached response (state is the JSON string)
            try:
                cached_data = json.loads(state)
                return Response(
                    content=cached_data["body"],
                    status_code=cached_data["status"],
                    media_type="application/json"
                )
            except json.JSONDecodeError:
                logger.error(f"Corrupt idempotency cache for key: {idem_key}")
                # Fall through and treat as new request

        # 2. Acquire the lock (Set NX)
        acquired = await idempotency_store.acquire_lock(idem_key, lock_ttl=30)
        if not acquired:
             return JSONResponse(
                status_code=409,
                content={"error": "request_in_progress", "message": "Concurrent request detected."}
            )

        # 3. Process the request
        try:
            response = await call_next(request)

            # 4. Read the response body so we can cache it
            response_body = b""
            async for chunk in response.body_iterator:
                response_body += chunk
            
            body_str = response_body.decode()

            # 5. Cache the response (only if successful or client error. Do NOT cache 500s)
            if response.status_code < 500:
                await idempotency_store.save_response(idem_key, response.status_code, body_str)
            else:
                # If server error, release lock so client can retry immediately
                await idempotency_store.release_lock(idem_key)

            # 6. Rebuild the response (since we consumed the body_iterator)
            return Response(
                content=response_body,
                status_code=response.status_code,
                headers=dict(response.headers),
                media_type=response.media_type
            )

        except Exception as e:
            # If an unhandled exception occurs, release the lock so it's not stuck for 30s
            await idempotency_store.release_lock(idem_key)
            raise e