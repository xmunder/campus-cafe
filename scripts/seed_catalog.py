from __future__ import annotations

import argparse
import os

import boto3

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


def main() -> None:
    parser = argparse.ArgumentParser(description="Carga el menú de Campus Café de forma idempotente.")
    parser.add_argument(
        "--table",
        default=os.environ.get("CATALOG_TABLE"),
        help="Nombre de la tabla Catalog obtenido de las salidas de SAM.",
    )
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "us-east-1"))
    parser.add_argument("--profile", help="Perfil con nombre de AWS CLI (opcional).")
    args = parser.parse_args()
    if not args.table:
        parser.error("Indica --table o asigna a CATALOG_TABLE el valor de salida CatalogTableName.")
    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    table = session.resource("dynamodb").Table(args.table)
    with table.batch_writer() as batch:
        for product in PRODUCTS:
            batch.put_item(Item=product)
    print(f"Se cargaron {len(PRODUCTS)} productos en {args.table}, región {args.region}.")


if __name__ == "__main__":
    main()
