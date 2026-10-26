from __future__ import annotations

import json
import logging
import time
import uuid
from collections.abc import Callable
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

logger = logging.getLogger("campus_cafe")
logger.setLevel(logging.INFO)


class ServiceFailure(Exception):
    def __init__(self, status_code: int, code: str, message: str) -> None:
        self.status_code = status_code
        self.code = code
        self.message = message


def request_id_for(request: Request) -> str:
    aws_event = request.scope.get("aws.event", {})
    event_context = aws_event.get("requestContext", {}) if isinstance(aws_event, dict) else {}
    return (
        event_context.get("requestId")
        or request.headers.get("x-amzn-requestid")
        or request.headers.get("x-request-id")
        or str(uuid.uuid4())
    )


def install_http_observability(app: FastAPI, operation: str) -> None:
    @app.middleware("http")
    async def log_request(request: Request, call_next: Callable[..., Any]) -> Any:
        started = time.perf_counter()
        request_id = request_id_for(request)
        request.state.request_id = request_id
        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
            response.headers["X-Request-Id"] = request_id
            return response
        finally:
            aws_event = request.scope.get("aws.event", {})
            path_params = (
                aws_event.get("pathParameters") or {} if isinstance(aws_event, dict) else {}
            )
            record = {
                "requestId": request_id,
                "operation": operation,
                "method": request.method,
                "path": request.url.path,
                "result": status_code,
                "durationMs": round((time.perf_counter() - started) * 1000, 2),
            }
            order_id = path_params.get("orderId")
            order_id = order_id or getattr(request.state, "order_id", None)
            if order_id:
                record["orderId"] = order_id
            logger.info(json.dumps(record, separators=(",", ":")))

    @app.exception_handler(ServiceFailure)
    async def handle_service_failure(request: Request, exc: ServiceFailure) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "code": exc.code,
                "message": exc.message,
                "requestId": request_id_for(request),
            },
        )

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=400,
            content={
                "code": "INVALID_REQUEST",
                "message": "The request does not match the API contract.",
                "requestId": request_id_for(request),
            },
        )

    @app.exception_handler(Exception)
    async def handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
        logger.exception(
            "Unhandled application error", extra={"requestId": request_id_for(request)}
        )
        return JSONResponse(
            status_code=500,
            content={
                "code": "INTERNAL_ERROR",
                "message": "An unexpected error occurred.",
                "requestId": request_id_for(request),
            },
        )
