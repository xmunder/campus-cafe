import httpx
import pytest
from test_orders import FakeCatalog, FakeRepository

from orders.app import create_app
from orders.service import OrderService


def make_client():
    service = OrderService(FakeRepository(), FakeCatalog())
    transport = httpx.ASGITransport(app=create_app(service_provider=lambda: service))
    return httpx.AsyncClient(transport=transport, base_url="http://test")


@pytest.mark.asyncio
async def test_create_replay_and_retrieve_http_contract():
    async with make_client() as client:
        payload = {"items": [{"productId": "coffee", "quantity": 2}]}
        first = await client.post("/orders", headers={"Idempotency-Key": "http-key"}, json=payload)
        assert first.status_code == 201
        assert first.headers["location"].endswith(first.json()["orderId"])
        assert first.headers["x-request-id"]
        assert first.json()["totalCents"] == 700

        replay = await client.post("/orders", headers={"Idempotency-Key": "http-key"}, json=payload)
        assert replay.status_code == 200
        assert replay.json() == first.json()

        retrieved = await client.get(f"/orders/{first.json()['orderId']}")
        assert retrieved.status_code == 200
        assert retrieved.json() == first.json()


@pytest.mark.asyncio
async def test_missing_key_and_price_override_return_contract_errors():
    async with make_client() as client:
        missing_key = await client.post(
            "/orders", json={"items": [{"productId": "coffee", "quantity": 1}]}
        )
        assert missing_key.status_code == 400
        assert missing_key.json()["code"] == "IDEMPOTENCY_KEY_REQUIRED"
        assert missing_key.json()["requestId"]

        override = await client.post(
            "/orders",
            headers={"Idempotency-Key": "override"},
            json={"items": [{"productId": "coffee", "quantity": 1, "priceCents": 1}]},
        )
        assert override.status_code == 400
        assert override.json()["code"] == "INVALID_REQUEST"
