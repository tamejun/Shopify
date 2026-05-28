import hashlib
import re
import time
import requests
from datetime import datetime, timezone


TIKTOK_EVENTS_URL = "https://business-api.tiktok.com/open_api/v1.3/event/track/"


class TikTokClient:
    def __init__(self, access_token: str, pixel_id: str):
        self.access_token = access_token
        self.pixel_id = pixel_id

    def track_purchase(self, order: dict) -> dict:
        customer = order.get("customer") or {}
        shipping = order.get("shipping_address") or {}
        items = order.get("line_items") or []
        order_number = str(order.get("order_number", ""))
        currency = order.get("currency", "SGD")
        total = float(order.get("total_price") or 0)
        created_at = order.get("created_at", "")

        if created_at:
            ts = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        else:
            ts = datetime.now(timezone.utc)
        timestamp = ts.strftime("%Y-%m-%dT%H:%M:%S+00:00")

        user = {}
        if customer.get("email"):
            user["email"] = [_sha256(customer["email"].lower().strip())]
        phone = shipping.get("phone") or customer.get("phone") or ""
        if phone:
            user["phone_number"] = [_sha256(_normalize_phone_e164(phone))]
        if customer.get("id"):
            user["external_id"] = [_sha256(str(customer["id"]))]

        contents = [
            {
                "content_id": str(i.get("product_id") or i.get("variant_id") or ""),
                "content_name": (i.get("name") or "")[:255],
                "quantity": i.get("quantity") or 1,
                "price": float(i.get("price") or 0),
            }
            for i in items
        ]

        payload = {
            "pixel_code": self.pixel_id,
            "event": "CompletePayment",
            "event_id": f"order_{order_number}",  # Shopify 앱 픽셀 이벤트와 중복 제거
            "timestamp": timestamp,
            "context": {"user": user, "ad": {}},
            "properties": {
                "order_id": order_number,
                "currency": currency,
                "value": total,
                "content_type": "product",
                "contents": contents,
            },
        }

        resp = requests.post(
            TIKTOK_EVENTS_URL,
            headers={"Access-Token": self.access_token, "Content-Type": "application/json"},
            json=payload,
            timeout=10,
        )
        resp.raise_for_status()
        result = resp.json()
        if result.get("code") != 0:
            raise TikTokError(f"TikTok Events API 오류 (code {result.get('code')}): {result.get('message')}")
        return result


class TikTokError(Exception):
    pass


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _normalize_phone_e164(phone: str) -> str:
    cleaned = re.sub(r"[^\d+]", "", phone.strip())
    if cleaned.startswith("+"):
        return cleaned
    if cleaned.startswith("00"):
        return "+" + cleaned[2:]
    return "+" + cleaned.lstrip("0")
