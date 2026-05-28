import re
import time
import requests


GA4_COLLECT_URL = "https://www.google-analytics.com/mp/collect"


class GoogleClient:
    def __init__(self, ga4_measurement_id: str, ga4_api_secret: str):
        self.ga4_measurement_id = ga4_measurement_id
        self.ga4_api_secret = ga4_api_secret

    def track_purchase(self, order: dict) -> dict:
        customer = order.get("customer") or {}
        items = order.get("line_items") or []
        order_number = str(order.get("order_number", ""))
        currency = order.get("currency", "SGD")
        total = float(order.get("total_price") or 0)
        shipping_price = float(((order.get("shipping_lines") or [{}])[0]).get("price") or 0)
        tax = float(order.get("total_tax") or 0)

        client_id = _stable_client_id(customer.get("email") or customer.get("id") or order_number)

        ga4_items = [
            {
                "item_id": str(i.get("product_id") or i.get("variant_id") or ""),
                "item_name": (i.get("name") or "")[:500],
                "quantity": i.get("quantity") or 1,
                "price": float(i.get("price") or 0),
                "currency": currency,
            }
            for i in items
        ]

        payload = {
            "client_id": client_id,
            "timestamp_micros": str(int(time.time() * 1_000_000)),
            "events": [
                {
                    "name": "purchase",
                    "params": {
                        "transaction_id": order_number,  # Shopify 앱 GA4 이벤트와 중복 제거
                        "value": total,
                        "currency": currency,
                        "shipping": shipping_price,
                        "tax": tax,
                        "items": ga4_items,
                        "engagement_time_msec": 100,
                    },
                }
            ],
        }

        if customer.get("id"):
            payload["user_id"] = str(customer["id"])

        resp = requests.post(
            GA4_COLLECT_URL,
            params={"measurement_id": self.ga4_measurement_id, "api_secret": self.ga4_api_secret},
            json=payload,
            timeout=10,
        )
        resp.raise_for_status()
        return {"status": "ok", "order_number": order_number}


class GoogleError(Exception):
    pass


def _stable_client_id(seed: str) -> str:
    import hashlib
    h = hashlib.md5(str(seed).encode()).hexdigest()
    return f"{h[:8]}.{h[8:16]}"
