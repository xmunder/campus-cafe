from __future__ import annotations

import hashlib
import json
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Protocol

from campus_cafe.http import ServiceFailure


class Catalog(Protocol):
    def get_product(self, product_id: str) -> dict[str, Any]: ...


class OrderRepository(Protocol):
    def get_idempotency(self, key: str) -> dict[str, Any] | None: ...

    def create_atomic(
        self, order_record: dict[str, Any], idempotency_record: dict[str, Any]
    ) -> bool: ...

    def get_order(self, order_id: str) -> dict[str, Any] | None: ...

    def cancel_order(self, order_id: str, updated_at: str) -> dict[str, Any] | None: ...


def normalized_fingerprint(items: list[dict[str, Any]]) -> str:
    canonical = sorted(
        ({"productId": item["productId"], "quantity": item["quantity"]} for item in items),
        key=lambda item: item["productId"],
    )
    encoded = json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class OrderService:
    def __init__(
        self,
        repository: OrderRepository,
        catalog: Catalog,
        clock: Callable[[], datetime] | None = None,
        id_factory: Callable[[], uuid.UUID] | None = None,
    ) -> None:
        self.repository = repository
        self.catalog = catalog
        self.clock = clock or (lambda: datetime.now(UTC))
        self.id_factory = id_factory or uuid.uuid4

    def create_order(
        self, idempotency_key: str, raw_items: list[dict[str, Any]]
    ) -> tuple[dict, bool]:
        if (
            not idempotency_key
            or len(idempotency_key) > 128
            or idempotency_key.strip() != idempotency_key
        ):
            raise ServiceFailure(
                400, "INVALID_IDEMPOTENCY_KEY", "A valid Idempotency-Key is required."
            )
        product_ids = [item["productId"] for item in raw_items]
        if len(set(product_ids)) != len(product_ids):
            raise ServiceFailure(400, "DUPLICATE_PRODUCT", "Each product may appear only once.")

        fingerprint = normalized_fingerprint(raw_items)
        previous = self.repository.get_idempotency(idempotency_key)
        if previous:
            if previous["fingerprint"] != fingerprint:
                raise ServiceFailure(
                    409, "IDEMPOTENCY_KEY_CONFLICT", "This key was used for another request."
                )
            return self._plain(previous["creationResponse"]), True

        snapshot_items = []
        total = 0
        for request_item in raw_items:
            product = self.catalog.get_product(request_item["productId"])
            if not product.get("available", False):
                raise ServiceFailure(409, "PRODUCT_UNAVAILABLE", "This product cannot be ordered.")
            if product.get("currency") != "USD":
                raise ServiceFailure(
                    503, "CATALOG_UNAVAILABLE", "The menu service returned an invalid currency."
                )
            unit_price = int(product["priceCents"])
            quantity = int(request_item["quantity"])
            snapshot_items.append(
                {
                    "productId": product["productId"],
                    "name": product["name"],
                    "quantity": quantity,
                    "unitPriceCents": unit_price,
                }
            )
            total += unit_price * quantity

        now = self.clock().isoformat(timespec="seconds").replace("+00:00", "Z")
        order_id = str(self.id_factory())
        response = {
            "orderId": order_id,
            "status": "CREATED",
            "items": snapshot_items,
            "totalCents": total,
            "currency": "USD",
            "createdAt": now,
            "updatedAt": now,
        }
        order_record = {"id": f"ORDER#{order_id}", "entityType": "ORDER", **response}
        idempotency_record = {
            "id": self._idempotency_storage_id(idempotency_key),
            "entityType": "IDEMPOTENCY",
            "fingerprint": fingerprint,
            "orderId": order_id,
            "creationResponse": response,
        }
        for attempt in range(3):
            if self.repository.create_atomic(order_record, idempotency_record):
                return response, False
            # La transacción perdedora vuelve a leer el registro ganador para resolver reintentos concurrentes.
            winner = self.repository.get_idempotency(idempotency_key)
            if winner:
                if winner["fingerprint"] != fingerprint:
                    raise ServiceFailure(
                        409, "IDEMPOTENCY_KEY_CONFLICT", "This key was used for another request."
                    )
                return self._plain(winner["creationResponse"]), True
            if attempt < 2:
                time.sleep(0.02 * (attempt + 1))
        raise ServiceFailure(
            503, "ORDER_STORE_UNAVAILABLE", "The order could not be saved. Retry safely."
        )

    def get_order(self, order_id: str) -> dict[str, Any]:
        order = self.repository.get_order(order_id)
        if not order:
            raise ServiceFailure(404, "ORDER_NOT_FOUND", "The requested order does not exist.")
        return self._public_order(order)

    def cancel_order(self, order_id: str) -> dict[str, Any]:
        now = self.clock().isoformat(timespec="seconds").replace("+00:00", "Z")
        order = self.repository.cancel_order(order_id, now)
        if not order:
            raise ServiceFailure(404, "ORDER_NOT_FOUND", "The requested order does not exist.")
        return self._public_order(order)

    @staticmethod
    def _public_order(order: dict[str, Any]) -> dict[str, Any]:
        return OrderService._plain(
            {
                key: order[key]
                for key in (
                    "orderId",
                    "status",
                    "items",
                    "totalCents",
                    "currency",
                    "createdAt",
                    "updatedAt",
                )
            }
        )

    @staticmethod
    def _plain(value: Any) -> Any:
        if isinstance(value, Decimal):
            return int(value) if value == value.to_integral_value() else float(value)
        if isinstance(value, dict):
            return {key: OrderService._plain(item) for key, item in value.items()}
        if isinstance(value, list):
            return [OrderService._plain(item) for item in value]
        return value

    @staticmethod
    def _idempotency_storage_id(key: str) -> str:
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        return f"IDEMPOTENCY#{digest}"
