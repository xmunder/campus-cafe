from __future__ import annotations

import hashlib
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from campus_cafe.http import ServiceFailure
from orders.service import OrderService


class FakeCatalog:
    def __init__(self, products=None, error=None):
        self.products = products or {
            "coffee": {
                "productId": "coffee",
                "name": "Coffee",
                "priceCents": 350,
                "currency": "USD",
                "available": True,
            },
            "muffin": {
                "productId": "muffin",
                "name": "Muffin",
                "priceCents": 375,
                "currency": "USD",
                "available": False,
            },
        }
        self.error = error
        self.calls = []

    def get_product(self, product_id):
        self.calls.append(product_id)
        if self.error:
            raise self.error
        if product_id not in self.products:
            raise ServiceFailure(400, "PRODUCT_NOT_FOUND", "The requested product does not exist.")
        return self.products[product_id]


class FakeRepository:
    def __init__(self):
        self.idempotency = {}
        self.orders = {}
        self.lock = threading.Lock()

    def get_idempotency(self, key):
        with self.lock:
            item_key = f"IDEMPOTENCY#{hashlib.sha256(key.encode('utf-8')).hexdigest()}"
            return self.idempotency.get(item_key)

    def create_atomic(self, order_record, idempotency_record):
        with self.lock:
            key = idempotency_record["id"]
            if key in self.idempotency:
                return False
            self.idempotency[key] = idempotency_record
            self.orders[order_record["orderId"]] = order_record
            return True

    def get_order(self, order_id):
        return self.orders.get(order_id)

    def cancel_order(self, order_id, updated_at):
        with self.lock:
            order = self.orders.get(order_id)
            if order is None:
                return None
            if order["status"] == "CREATED":
                order["status"] = "CANCELLED"
                order["updatedAt"] = updated_at
            return order.copy()


def setup_service(catalog=None, repository=None):
    repo = repository or FakeRepository()
    menu = catalog or FakeCatalog()
    return OrderService(repo, menu), repo, menu


def test_server_calculates_snapshot_and_replays_without_repricing():
    service, _, catalog = setup_service()
    body = [{"productId": "coffee", "quantity": 2}]
    created, replay = service.create_order("submission-1", body)
    assert replay is False
    assert created["totalCents"] == 700
    assert created["items"] == [
        {"productId": "coffee", "name": "Coffee", "quantity": 2, "unitPriceCents": 350}
    ]
    catalog.products["coffee"]["priceCents"] = 999
    repeated, replay = service.create_order("submission-1", body)
    assert replay is True
    assert repeated == created
    assert catalog.calls == ["coffee"]


def test_normalized_request_order_does_not_change_idempotency_fingerprint():
    service, _, _ = setup_service()
    created, _ = service.create_order("same-order", [{"productId": "coffee", "quantity": 1}])
    replay, was_replay = service.create_order(
        "same-order", [{"quantity": 1, "productId": "coffee"}]
    )
    assert was_replay and replay == created


def test_conflicting_key_and_duplicate_product_lines_are_rejected():
    service, _, catalog = setup_service()
    service.create_order("same-key", [{"productId": "coffee", "quantity": 1}])
    with pytest.raises(ServiceFailure) as conflict:
        service.create_order("same-key", [{"productId": "coffee", "quantity": 2}])
    assert conflict.value.status_code == 409
    with pytest.raises(ServiceFailure) as duplicate:
        service.create_order(
            "new-key",
            [{"productId": "coffee", "quantity": 1}, {"productId": "coffee", "quantity": 2}],
        )
    assert duplicate.value.status_code == 400
    assert catalog.calls == ["coffee"]


def test_unavailable_product_and_catalog_failure_do_not_persist_order():
    service, repo, _ = setup_service()
    with pytest.raises(ServiceFailure) as unavailable:
        service.create_order("unavailable", [{"productId": "muffin", "quantity": 1}])
    assert unavailable.value.status_code == 409
    assert repo.orders == {}

    failing_catalog = FakeCatalog(error=ServiceFailure(503, "CATALOG_UNAVAILABLE", "timeout"))
    service, repo, _ = setup_service(catalog=failing_catalog)
    with pytest.raises(ServiceFailure) as failure:
        service.create_order("catalog-down", [{"productId": "coffee", "quantity": 1}])
    assert failure.value.status_code == 503
    assert repo.orders == {}
    assert repo.idempotency == {}


def test_concurrent_identical_submissions_create_one_order():
    service, repo, _ = setup_service()
    items = [{"productId": "coffee", "quantity": 2}]
    with ThreadPoolExecutor(max_workers=5) as executor:
        results = list(executor.map(lambda _: service.create_order("concurrent", items), range(5)))
    assert len({result[0]["orderId"] for result in results}) == 1
    assert len(repo.orders) == 1
    assert len(repo.idempotency) == 1


def test_cancellation_persists_and_repeats_without_another_transition():
    service, _, _ = setup_service()
    created, _ = service.create_order("cancel-me", [{"productId": "coffee", "quantity": 1}])
    cancelled = service.cancel_order(created["orderId"])
    repeated = service.cancel_order(created["orderId"])
    assert cancelled["status"] == "CANCELLED"
    assert repeated == cancelled
    with pytest.raises(ServiceFailure) as absent:
        service.cancel_order("missing")
    assert absent.value.status_code == 404
