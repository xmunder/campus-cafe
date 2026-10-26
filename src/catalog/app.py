from __future__ import annotations

import logging
import os
from typing import Any

import boto3
from fastapi import FastAPI
from mangum import Mangum

from campus_cafe.http import ServiceFailure, install_http_observability

logger = logging.getLogger("campus_cafe.catalog")


def _table() -> Any:
    return boto3.resource("dynamodb").Table(os.environ["CATALOG_TABLE"])


def _product(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "productId": item["productId"],
        "name": item["name"],
        "priceCents": int(item["priceCents"]),
        "currency": item["currency"],
        "available": bool(item["available"]),
    }


def create_app(table_provider=_table) -> FastAPI:
    app = FastAPI(title="Campus Café: Catálogo", docs_url=None, redoc_url=None)
    install_http_observability(app, "catalog")

    @app.get("/products")
    def list_products() -> dict[str, list[dict[str, Any]]]:
        try:
            table = table_provider()
            items: list[dict[str, Any]] = []
            result = table.scan(ConsistentRead=True)
            items.extend(result.get("Items", []))
            while result.get("LastEvaluatedKey"):
                result = table.scan(
                    ConsistentRead=True,
                    ExclusiveStartKey=result["LastEvaluatedKey"],
                )
                items.extend(result.get("Items", []))
            return {
                "items": [_product(row) for row in sorted(items, key=lambda row: row["productId"])]
            }
        except Exception as exc:
            logger.exception("Catalog table read failed")
            raise ServiceFailure(
                503, "CATALOG_UNAVAILABLE", "The menu is temporarily unavailable."
            ) from exc

    @app.get("/products/{productId}")
    def get_product(productId: str) -> dict[str, Any]:
        try:
            response = table_provider().get_item(Key={"productId": productId}, ConsistentRead=True)
        except Exception as exc:
            logger.exception("Catalog product lookup failed")
            raise ServiceFailure(
                503, "CATALOG_UNAVAILABLE", "The menu is temporarily unavailable."
            ) from exc
        item = response.get("Item")
        if not item:
            raise ServiceFailure(404, "PRODUCT_NOT_FOUND", "The requested product does not exist.")
        return _product(item)

    return app


app = create_app()
handler = Mangum(app, lifespan="off")
