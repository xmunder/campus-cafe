from __future__ import annotations

import argparse
import hashlib
import json
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import boto3
import httpx


def expect(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def main() -> None:
    parser = argparse.ArgumentParser(description="Ejecuta la aceptación de la HTTP API desplegada de Campus Café.")
    parser.add_argument("--api-url", required=True, help="Valor ApiBaseUrl generado por sam deploy.")
    parser.add_argument(
        "--orders-table", help="Valor OrdersTableName; habilita la verificación de concurrencia persistida."
    )
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument("--profile", help="Perfil con nombre de AWS CLI (opcional).")
    args = parser.parse_args()
    api = args.api_url.rstrip("/")
    client = httpx.Client(base_url=api, timeout=10)

    started = time.perf_counter()
    menu_response = client.get("/products")
    menu_ms = (time.perf_counter() - started) * 1000
    expect(menu_response.status_code == 200, f"GET /products devolvió {menu_response.status_code}")
    products = menu_response.json()["items"]
    expect(len(products) >= 5, "Catalog debe contener al menos cinco productos")
    available = next((p for p in products if p["available"]), None)
    unavailable = next((p for p in products if not p["available"]), None)
    expect(
        available is not None and unavailable is not None,
        "Se necesitan productos cargados que estén disponibles y no disponibles",
    )

    first = client.get(f"/products/{available['productId']}")
    expect(first.status_code == 200, "Falló GET /products/{id}")
    missing_product = client.get("/products/no-such-product")
    expect(missing_product.status_code == 404, "Un producto desconocido debe devolver 404")

    request_body = {"items": [{"productId": available["productId"], "quantity": 2}]}
    key = f"acceptance-{uuid.uuid4()}"
    created = client.post("/orders", headers={"Idempotency-Key": key}, json=request_body)
    expect(
        created.status_code == 201, f"La primera creación devolvió {created.status_code}: {created.text}"
    )
    order = created.json()
    expect(
        created.headers.get("Location", "").endswith(order["orderId"]),
        "La primera creación debe devolver Location",
    )
    expected_total = int(available["priceCents"]) * 2
    expect(
        order["totalCents"] == expected_total,
        "El total calculado por el servidor no coincide con el precio del catálogo",
    )

    replay = client.post("/orders", headers={"Idempotency-Key": key}, json=request_body)
    expect(
        replay.status_code == 200 and replay.json() == order,
        "Al repetir la solicitud no se devolvió el resultado original",
    )
    conflict = client.post(
        "/orders",
        headers={"Idempotency-Key": key},
        json={"items": [{"productId": available["productId"], "quantity": 1}]},
    )
    expect(conflict.status_code == 409, "La misma clave con otra solicitud debe devolver 409")

    price_override = client.post(
        "/orders",
        headers={"Idempotency-Key": f"override-{uuid.uuid4()}"},
        json={"items": [{"productId": available["productId"], "quantity": 1, "priceCents": 1}]},
    )
    expect(price_override.status_code == 400, "Debe rechazarse un precio enviado por el cliente")
    unknown_order_product = client.post(
        "/orders",
        headers={"Idempotency-Key": f"unknown-{uuid.uuid4()}"},
        json={"items": [{"productId": "no-such-product", "quantity": 1}]},
    )
    expect(unknown_order_product.status_code == 400, "Un producto desconocido debe devolver 400")
    unavailable_order = client.post(
        "/orders",
        headers={"Idempotency-Key": f"unavailable-{uuid.uuid4()}"},
        json={"items": [{"productId": unavailable["productId"], "quantity": 1}]},
    )
    expect(unavailable_order.status_code == 409, "Un producto no disponible debe devolver 409")

    persisted = client.get(f"/orders/{order['orderId']}")
    expect(
        persisted.status_code == 200 and persisted.json() == order,
        "No fue posible recuperar el pedido persistido",
    )
    cancelled = client.post(f"/orders/{order['orderId']}/cancel")
    cancelled_again = client.post(f"/orders/{order['orderId']}/cancel")
    expect(
        cancelled.status_code == 200 and cancelled.json()["status"] == "CANCELLED",
        "Falló la cancelación",
    )
    expect(cancelled_again.json() == cancelled.json(), "La cancelación repetida modificó el pedido")
    absent_cancel = client.post(f"/orders/{uuid.uuid4()}/cancel")
    expect(absent_cancel.status_code == 404, "Cancelar un pedido inexistente debe devolver 404")

    concurrent_key = f"concurrent-{uuid.uuid4()}"

    def create_same_order() -> httpx.Response:
        return client.post(
            "/orders", headers={"Idempotency-Key": concurrent_key}, json=request_body
        )

    with ThreadPoolExecutor(max_workers=5) as pool:
        concurrent = list(pool.map(lambda _: create_same_order(), range(5)))
    expect(
        all(response.status_code in (200, 201) for response in concurrent),
        "Falló una solicitud concurrente",
    )
    concurrent_ids = {response.json()["orderId"] for response in concurrent}
    expect(len(concurrent_ids) == 1, "Las solicitudes concurrentes idénticas crearon más de un pedido")
    concurrent_id = next(iter(concurrent_ids))
    if args.orders_table:
        session = boto3.Session(profile_name=args.profile, region_name=args.region)
        table = session.resource("dynamodb").Table(args.orders_table)
        order_item = table.get_item(Key={"id": f"ORDER#{concurrent_id}"}, ConsistentRead=True).get(
            "Item"
        )
        key_hash = hashlib.sha256(concurrent_key.encode("utf-8")).hexdigest()
        idem_item = table.get_item(Key={"id": f"IDEMPOTENCY#{key_hash}"}, ConsistentRead=True).get(
            "Item"
        )
        expect(
            order_item is not None and idem_item is not None,
            "No se persistieron atómicamente los registros del pedido y de idempotencia",
        )
        expect(idem_item["orderId"] == concurrent_id, "El registro de idempotencia apunta a otro pedido")

    # Tres lecturas de calentamiento, seguidas de las 20 mediciones secuenciales requeridas.
    warmups = []
    for _ in range(3):
        start = time.perf_counter()
        response = client.get(f"/orders/{order['orderId']}")
        warmups.append((time.perf_counter() - start) * 1000)
        expect(response.status_code == 200, "Falló la lectura de calentamiento del pedido")
    observations = []
    for _ in range(20):
        start = time.perf_counter()
        response = client.get(f"/orders/{order['orderId']}")
        observations.append((time.perf_counter() - start) * 1000)
        expect(response.status_code == 200, "Falló la lectura cronometrada del pedido")
    ordered = sorted(observations)
    p95 = ordered[max(0, int(0.95 * len(ordered)) - 1)]
    print(
        json.dumps(
            {
                "result": "PASS",
                "region": args.region,
                "apiUrl": api,
                "menuRequestMs": round(menu_ms, 2),
                "firstInvocationMs": round(menu_ms, 2),
                "warmupOrderReadMs": [round(value, 2) for value in warmups],
                "sequentialOrderReadMs": [round(value, 2) for value in observations],
                "p95OrderReadMs": round(p95, 2),
                "p95TargetMs": 2000,
                "p95TargetMet": p95 < 2000,
                "concurrentRequestStatuses": [response.status_code for response in concurrent],
                "concurrentOrderId": concurrent_id,
                "persistedConcurrencyRecordsVerified": bool(args.orders_table),
                "note": (
                    "La primera solicitud es una latencia observada. Confirma la inicialización "
                    "en los registros de Lambda antes de considerarla un arranque en frío."
                ),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
