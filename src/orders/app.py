from __future__ import annotations

import functools
import logging
import os
from collections.abc import Callable
from uuid import UUID

from fastapi import FastAPI, Header, Request
from fastapi.responses import JSONResponse
from mangum import Mangum

from campus_cafe.http import ServiceFailure, install_http_observability
from orders.adapters import CatalogHttpClient, DynamoOrderRepository
from orders.models import CreateOrderRequest
from orders.service import OrderService

logger = logging.getLogger("campus_cafe.orders")


@functools.lru_cache(maxsize=1)
def _default_service() -> OrderService:
    return OrderService(
        repository=DynamoOrderRepository(),
        catalog=CatalogHttpClient(
            base_url=os.environ["CATALOG_API_URL"],
            timeout_seconds=float(os.environ.get("CATALOG_TIMEOUT_SECONDS", "2")),
        ),
    )


def create_app(service_provider: Callable[[], OrderService] = _default_service) -> FastAPI:
    app = FastAPI(title="Campus Café: Pedidos", docs_url=None, redoc_url=None)
    install_http_observability(app, "orders")

    @app.post("/orders", name="create_order")
    def create_order(
        body: CreateOrderRequest,
        request: Request,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    ) -> JSONResponse:
        if not idempotency_key:
            raise ServiceFailure(
                400, "IDEMPOTENCY_KEY_REQUIRED", "Idempotency-Key header is required."
            )
        result, replay = service_provider().create_order(
            idempotency_key,
            [item.model_dump() for item in body.items],
        )
        request.state.order_id = result["orderId"]
        headers = {}
        if not replay:
            headers["Location"] = str(request.url_for("get_order", orderId=result["orderId"]))
        return JSONResponse(status_code=200 if replay else 201, content=result, headers=headers)

    @app.get("/orders/{orderId}", name="get_order")
    def get_order(orderId: UUID, request: Request) -> dict:
        result = service_provider().get_order(str(orderId))
        request.state.order_id = result["orderId"]
        return result

    @app.post("/orders/{orderId}/cancel")
    def cancel_order(orderId: UUID, request: Request) -> dict:
        result = service_provider().cancel_order(str(orderId))
        request.state.order_id = result["orderId"]
        return result

    return app


app = create_app()
handler = Mangum(app, lifespan="off")
