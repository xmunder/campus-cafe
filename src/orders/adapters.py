from __future__ import annotations

import logging
import os
from typing import Any

import boto3
import httpx
from botocore.exceptions import ClientError

from campus_cafe.http import ServiceFailure

logger = logging.getLogger("campus_cafe.orders.adapters")


class CatalogHttpClient:
    def __init__(self, base_url: str, timeout_seconds: float) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds

    def get_product(self, product_id: str) -> dict[str, Any]:
        try:
            response = httpx.get(
                f"{self.base_url}/products/{product_id}",
                timeout=httpx.Timeout(self.timeout_seconds),
            )
        except (httpx.TimeoutException, httpx.ConnectError) as exc:
            raise ServiceFailure(
                503, "CATALOG_UNAVAILABLE", "The menu service is temporarily unavailable."
            ) from exc
        except httpx.HTTPError as exc:
            raise ServiceFailure(
                503, "CATALOG_UNAVAILABLE", "The menu service is temporarily unavailable."
            ) from exc

        if response.status_code == 404:
            raise ServiceFailure(400, "PRODUCT_NOT_FOUND", "The requested product does not exist.")
        if response.status_code >= 500:
            raise ServiceFailure(
                503, "CATALOG_UNAVAILABLE", "The menu service is temporarily unavailable."
            )
        if response.status_code != 200:
            raise ServiceFailure(
                503, "CATALOG_UNAVAILABLE", "The menu service returned an invalid response."
            )
        try:
            product = response.json()
            if not all(key in product for key in ("productId", "name", "priceCents", "available")):
                raise ValueError("Missing product fields")
            return product
        except (ValueError, TypeError) as exc:
            raise ServiceFailure(
                503, "CATALOG_UNAVAILABLE", "The menu service returned an invalid response."
            ) from exc


class DynamoOrderRepository:
    def __init__(self, table_name: str | None = None, dynamodb=None) -> None:
        self.dynamodb = dynamodb or boto3.resource("dynamodb")
        self.table = self.dynamodb.Table(table_name or os.environ["ORDERS_TABLE"])
        self.client = self.table.meta.client

    def get_idempotency(self, key: str) -> dict[str, Any] | None:
        try:
            response = self.table.get_item(
                Key={"id": self._id_for_key(key)},
                ConsistentRead=True,
            )
            return response.get("Item")
        except ClientError as exc:
            raise ServiceFailure(
                503, "ORDER_STORE_UNAVAILABLE", "The order store is temporarily unavailable."
            ) from exc

    def create_atomic(
        self, order_record: dict[str, Any], idempotency_record: dict[str, Any]
    ) -> bool:
        try:
            self.client.transact_write_items(
                TransactItems=[
                    {
                        "Put": {
                            "TableName": self.table.name,
                            "Item": order_record,
                            "ConditionExpression": "attribute_not_exists(#id)",
                            "ExpressionAttributeNames": {"#id": "id"},
                        }
                    },
                    {
                        "Put": {
                            "TableName": self.table.name,
                            "Item": idempotency_record,
                            "ConditionExpression": "attribute_not_exists(#id)",
                            "ExpressionAttributeNames": {"#id": "id"},
                        }
                    },
                ]
            )
            return True
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code")
            logger.error(
                "DynamoDB order transaction failed: code=%s cancellationReasons=%s",
                code,
                exc.response.get("CancellationReasons"),
            )
            if code in {
                "TransactionCanceledException",
                "TransactionConflictException",
                "ConditionalCheckFailedException",
            }:
                return False
            raise ServiceFailure(
                503, "ORDER_STORE_UNAVAILABLE", "The order store is temporarily unavailable."
            ) from exc

    def get_order(self, order_id: str) -> dict[str, Any] | None:
        try:
            response = self.table.get_item(Key={"id": f"ORDER#{order_id}"}, ConsistentRead=True)
            return response.get("Item")
        except ClientError as exc:
            raise ServiceFailure(
                503, "ORDER_STORE_UNAVAILABLE", "The order store is temporarily unavailable."
            ) from exc

    def cancel_order(self, order_id: str, updated_at: str) -> dict[str, Any] | None:
        key = {"id": f"ORDER#{order_id}"}
        try:
            existing = self.table.get_item(Key=key, ConsistentRead=True).get("Item")
        except ClientError as exc:
            raise ServiceFailure(
                503, "ORDER_STORE_UNAVAILABLE", "The order store is temporarily unavailable."
            ) from exc
        if not existing:
            return None
        if existing["status"] == "CANCELLED":
            return existing
        try:
            response = self.table.update_item(
                Key=key,
                UpdateExpression="SET #status = :cancelled, #updatedAt = :updatedAt",
                ConditionExpression="attribute_exists(#id) AND #status = :created",
                ExpressionAttributeNames={
                    "#id": "id",
                    "#status": "status",
                    "#updatedAt": "updatedAt",
                },
                ExpressionAttributeValues={
                    ":created": "CREATED",
                    ":cancelled": "CANCELLED",
                    ":updatedAt": updated_at,
                },
                ReturnValues="ALL_NEW",
            )
            return response["Attributes"]
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                latest = self.table.get_item(Key=key, ConsistentRead=True).get("Item")
                if latest and latest.get("status") == "CANCELLED":
                    return latest
                return latest
            raise ServiceFailure(
                503, "ORDER_STORE_UNAVAILABLE", "The order store is temporarily unavailable."
            ) from exc

    @staticmethod
    def _id_for_key(key: str) -> str:
        import hashlib

        return f"IDEMPOTENCY#{hashlib.sha256(key.encode('utf-8')).hexdigest()}"
