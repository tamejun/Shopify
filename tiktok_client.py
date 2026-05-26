import hashlib
import hmac
import json
import time
import requests
from datetime import datetime, timezone, timedelta
from urllib.parse import urlencode


TIKTOK_EVENTS_URL = "https://business-api.tiktok.com/open_api/v1.3/event/track/"
TIKTOK_SHOP_BASE = "https://open-api.tiktokglobal.com"


class TikTokClient:
    def __init__(
        self,
        access_token: str,
        pixel_id: str,
        app_key: str | None = None,
        app_secret: str | None = None,
    ):
        self.access_token = access_token
        self.pixel_id = pixel_id
        self.app_key = app_key
        self.app_secret = app_secret

    # ── Events API ────────────────────────────────────────────────────────────

    def track_purchase(self, order: dict, tracking_number: str = "") -> dict:
        customer = order.get("customer") or {}
        shipping = order.get("shipping_address") or {}
        items = order.get("line_items") or []
        order_number = str(order.get("order_number", ""))
        currency = order.get("currency", "SGD")
        total = float(order.get("total_price") or 0)
        created_at = order.get("created_at", "")

        event_time = int(
            datetime.fromisoformat(created_at.replace("Z", "+00:00")).timestamp()
            if created_at
            else time.time()
        )

        user = {}
        if customer.get("email"):
            user["email"] = [_sha256(customer["email"].lower().strip())]
        phone = shipping.get("phone") or customer.get("phone") or ""
        if phone:
            user["phone_number"] = [_sha256(_strip_phone(phone))]

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
            "event": "Purchase",
            "event_time": event_time,
            "context": {
                "user": user,
                "ad": {},
            },
            "properties": {
                "order_id": order_number,
                "currency": currency,
                "value": total,
                "contents": contents,
            },
        }
        if tracking_number:
            payload["properties"]["order_id"] = order_number

        resp = requests.post(
            TIKTOK_EVENTS_URL,
            headers={"Access-Token": self.access_token, "Content-Type": "application/json"},
            json=payload,
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json()

    # ── TikTok Shop API ───────────────────────────────────────────────────────

    def _shop_headers(self, path: str, params: dict, body: str = "") -> dict:
        if not (self.app_key and self.app_secret):
            raise TikTokError("TikTok Shop 자격증명(app_key/app_secret)이 설정되지 않았습니다.")
        timestamp = str(int(time.time()))
        sign = _compute_shop_signature(self.app_secret, path, params, timestamp, body)
        return {
            "Content-Type": "application/json",
            "x-tts-access-token": self.access_token,
        }, {**params, "app_key": self.app_key, "timestamp": timestamp, "sign": sign}

    def get_shop_orders(self, days_back: int = 1) -> list[dict]:
        if not (self.app_key and self.app_secret):
            return []
        path = "/order/202309/orders/search"
        since = int((datetime.now(timezone.utc) - timedelta(days=days_back)).timestamp())
        body_data = {
            "order_status": 100,  # AWAITING_SHIPMENT
            "create_time_ge": since,
        }
        body_str = json.dumps(body_data)
        params = {"version": "202309"}
        headers, signed_params = self._shop_headers(path, params, body_str)
        resp = requests.post(
            f"{TIKTOK_SHOP_BASE}{path}",
            headers=headers,
            params=signed_params,
            data=body_str,
            timeout=15,
        )
        if not resp.ok:
            raise TikTokError(f"TikTok Shop 주문 조회 실패 (HTTP {resp.status_code}): {resp.text}")
        data = resp.json()
        if data.get("code") != 0:
            raise TikTokError(f"TikTok Shop 오류: {data.get('message')}")
        return (data.get("data") or {}).get("orders") or []

    def sync_products_to_shop(self, products: list[dict]) -> list[dict]:
        if not (self.app_key and self.app_secret):
            raise TikTokError("TikTok Shop 자격증명(app_key/app_secret)이 설정되지 않았습니다.")
        results = []
        for product in products:
            try:
                result = self._upsert_shop_product(product)
                results.append({"product_id": product.get("id"), "status": "ok", "result": result})
            except TikTokError as e:
                results.append({"product_id": product.get("id"), "status": "error", "error": str(e)})
        return results

    def _upsert_shop_product(self, product: dict) -> dict:
        path = "/product/202309/products"
        variant = (product.get("variants") or [{}])[0]
        images = [{"uri": img["src"]} for img in (product.get("images") or []) if img.get("src")]
        body_data = {
            "title": product.get("title", ""),
            "description": product.get("body_html", "") or product.get("title", ""),
            "category_id": "600001",  # 기본값: 기타 상품 — 실제 카테고리 ID로 교체 필요
            "brand_id": "",
            "images": images[:9],
            "skus": [
                {
                    "sales_attributes": [],
                    "seller_sku": str(variant.get("sku") or variant.get("id") or ""),
                    "original_price": str(variant.get("price") or "0"),
                    "quantity_list": [{"warehouse_id": "", "quantity": variant.get("inventory_quantity") or 0}],
                }
            ],
        }
        body_str = json.dumps(body_data)
        params = {"version": "202309"}
        headers, signed_params = self._shop_headers(path, params, body_str)
        resp = requests.post(
            f"{TIKTOK_SHOP_BASE}{path}",
            headers=headers,
            params=signed_params,
            data=body_str,
            timeout=15,
        )
        if not resp.ok:
            raise TikTokError(f"TikTok Shop 상품 등록 실패 (HTTP {resp.status_code}): {resp.text}")
        data = resp.json()
        if data.get("code") != 0:
            raise TikTokError(f"TikTok Shop 상품 오류: {data.get('message')}")
        return data.get("data") or {}


class TikTokError(Exception):
    pass


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _strip_phone(phone: str) -> str:
    import re
    cleaned = re.sub(r"[^\d+]", "", phone.strip())
    if not cleaned.startswith("+"):
        cleaned = "+" + cleaned.lstrip("0")
    return cleaned


def _compute_shop_signature(secret: str, path: str, params: dict, timestamp: str, body: str) -> str:
    sorted_params = "".join(f"{k}{v}" for k, v in sorted(params.items()))
    base = f"{secret}{path}{sorted_params}{timestamp}{body}{secret}"
    return hmac.new(secret.encode(), base.encode(), hashlib.sha256).hexdigest()
