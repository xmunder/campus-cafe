from __future__ import annotations

import copy
import hashlib
import sys
import threading
import time
from pathlib import Path

import uvicorn
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import FileResponse

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from catalog.app import create_app as create_catalog_app
from orders.adapters import CatalogHttpClient
from orders.app import create_app as create_orders_app
from orders.service import OrderService

FRONTEND_ORIGIN = "http://localhost:4173"
PRODUCTS = [
    {
        "productId": "coffee",
        "name": "House Coffee",
        "priceCents": 350,
        "currency": "USD",
        "available": True,
    },
    {
        "productId": "latte",
        "name": "Oat Milk Latte",
        "priceCents": 475,
        "currency": "USD",
        "available": True,
    },
    {
        "productId": "tea",
        "name": "Mint Tea",
        "priceCents": 300,
        "currency": "USD",
        "available": True,
    },
    {
        "productId": "croissant",
        "name": "Butter Croissant",
        "priceCents": 425,
        "currency": "USD",
        "available": True,
    },
    {
        "productId": "cookie",
        "name": "Chocolate Cookie",
        "priceCents": 250,
        "currency": "USD",
        "available": True,
    },
    {
        "productId": "muffin",
        "name": "Blueberry Muffin",
        "priceCents": 375,
        "currency": "USD",
        "available": False,
    },
]


class MemoryCatalogTable:
    def __init__(self):
        self.items = {product["productId"]: copy.deepcopy(product) for product in PRODUCTS}

    def scan(self, **_kwargs):
        return {"Items": [copy.deepcopy(item) for item in self.items.values()]}

    def get_item(self, Key, **_kwargs):
        item = self.items.get(Key["productId"])
        return {"Item": copy.deepcopy(item)} if item else {}


class MemoryOrderRepository:
    def __init__(self):
        self.orders = {}
        self.idempotency = {}
        self.lock = threading.RLock()

    @staticmethod
    def _idempotency_id(key):
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        return f"IDEMPOTENCY#{digest}"

    def get_idempotency(self, key):
        with self.lock:
            item = self.idempotency.get(self._idempotency_id(key))
            return copy.deepcopy(item) if item else None

    def create_atomic(self, order_record, idempotency_record):
        with self.lock:
            key = idempotency_record["id"]
            if key in self.idempotency or order_record["orderId"] in self.orders:
                return False
            self.orders[order_record["orderId"]] = copy.deepcopy(order_record)
            self.idempotency[key] = copy.deepcopy(idempotency_record)
            return True

    def get_order(self, order_id):
        with self.lock:
            item = self.orders.get(order_id)
            return copy.deepcopy(item) if item else None

    def cancel_order(self, order_id, updated_at):
        with self.lock:
            item = self.orders.get(order_id)
            if not item:
                return None
            if item["status"] == "CREATED":
                item["status"] = "CANCELLED"
                item["updatedAt"] = updated_at
            return copy.deepcopy(item)


def with_local_cors(app):
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[FRONTEND_ORIGIN],
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Content-Type", "Idempotency-Key"],
        expose_headers=["Location"],
    )
    return app


def start_server(app, port):
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="info", access_log=False)
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    return server, thread


def wait_until_ready(url):
    import httpx

    for _ in range(50):
        try:
            if httpx.get(url, timeout=0.5).status_code == 200:
                return
        except httpx.HTTPError:
            time.sleep(0.1)
    raise RuntimeError(f"El servicio local no estuvo listo a tiempo: {url}")


def main():
    catalog_table = MemoryCatalogTable()
    order_repository = MemoryOrderRepository()
    catalog_app = with_local_cors(create_catalog_app(table_provider=lambda: catalog_table))
    orders_service = OrderService(
        repository=order_repository,
        catalog=CatalogHttpClient("http://127.0.0.1:8001", timeout_seconds=2),
    )
    orders_app = with_local_cors(create_orders_app(service_provider=lambda: orders_service))
    openapi_file = Path(__file__).resolve().parents[1] / "openapi.yaml"

    @orders_app.get("/docs", include_in_schema=False)
    def local_api_docs():
        return get_swagger_ui_html(
            openapi_url="/openapi.yaml",
            title="Campus Café: documentación de la API",
        )

    @orders_app.get("/openapi.yaml", include_in_schema=False)
    def local_openapi_spec():
        return FileResponse(openapi_file, media_type="application/yaml")

    # Imita el enrutamiento de API Gateway bajo una sola URL base para el navegador y las pruebas de aceptación.
    orders_app.mount("/", catalog_app)

    catalog_server, catalog_thread = start_server(catalog_app, 8001)
    try:
        wait_until_ready("http://127.0.0.1:8001/products")
        orders_server, orders_thread = start_server(orders_app, 8002)
        print("API local de Catalog: http://127.0.0.1:8001")
        print("API local de Orders:  http://127.0.0.1:8002")
        print("Interfaz web:         http://localhost:4173/?api=http://127.0.0.1:8002")
        print(
            "Los datos están en memoria y se borrarán al detener el proceso. Pulsa Ctrl+C para salir."
        )
        try:
            while catalog_thread.is_alive() and orders_thread.is_alive():
                time.sleep(0.5)
        except KeyboardInterrupt:
            print("Deteniendo las API locales…")
            orders_server.should_exit = True
    finally:
        catalog_server.should_exit = True


if __name__ == "__main__":
    main()
