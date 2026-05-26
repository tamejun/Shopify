import hashlib
import hmac
import json
import re
import time
import requests
from datetime import datetime, timezone, timedelta


TIKTOK_EVENTS_URL = "https://business-api.tiktok.com/open_api/v1.3/event/track/"
TIKTOK_SHOP_BASE = "https://open-api.tiktokglobalshop.com"


class TikTokClient:
    def __init__(
        self,
        access_token: str,
        pixel_id: str,
        app_key: str | None = None,
        app_secret: str | None = None,
        shop_cipher: str | None = None,
    ):
        self.access_token = access_token
        self.pixel_id = pixel_id
        self.app_key = app_key
        self.app_secret = app_secret
        self.shop_cipher = shop_cipher  # 샵별 암호키 — TikTok Shop OAuth 완료 시 발급

    # ── Events API ────────────────────────────────────────────────────────────

    def track_purchase(self, order: dict, tracking_number: str = "") -> dict:
        customer = order.get("customer") or {}
        shipping = order.get("shipping_address") or {}
        items = order.get("line_items") or []
        order_number = str(order.get("order_number", ""))
        currency = order.get("currency", "SGD")
        total = float(order.get("total_price") or 0)
        created_at = order.get("created_at", "")

        # ISO 8601 타임스탬프 형식 (TikTok API 요구사항)
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
            "event": "CompletePayment",  # TikTok의 구매 이벤트 표준명
            "event_id": f"order_{order_number}",  # 브라우저 픽셀과 중복 제거용
            "timestamp": timestamp,
            "context": {
                "user": user,
                "ad": {},
            },
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

    # ── TikTok Shop API ───────────────────────────────────────────────────────

    def _require_shop_creds(self):
        if not (self.app_key and self.app_secret):
            raise TikTokError("TikTok Shop 자격증명(TIKTOK_SHOP_APP_KEY, TIKTOK_SHOP_APP_SECRET)이 설정되지 않았습니다.")
        if not self.shop_cipher:
            raise TikTokError("TIKTOK_SHOP_CIPHER가 설정되지 않았습니다. TikTok Shop OAuth 완료 후 shop_cipher를 발급받으세요.")

    def _sign_request(self, path: str, params: dict, body: str = "") -> dict:
        self._require_shop_creds()
        base_params = {
            **params,
            "app_key": self.app_key,
            "timestamp": str(int(time.time())),
            "version": "202309",
        }
        if self.shop_cipher:
            base_params["shop_cipher"] = self.shop_cipher
        excluded = {"sign", "access_token"}
        sorted_str = "".join(f"{k}{v}" for k, v in sorted(base_params.items()) if k not in excluded)
        sign_input = f"{self.app_secret}{path}{sorted_str}{body}{self.app_secret}"
        signature = hmac.new(
            self.app_secret.encode("utf-8"),
            sign_input.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest().upper()
        return {**base_params, "sign": signature}

    def _shop_call(self, method: str, path: str, params: dict = {}, body_data: dict | None = None) -> dict:
        body_str = json.dumps(body_data, separators=(",", ":")) if body_data else ""
        signed_params = self._sign_request(path, params, body_str)
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.access_token}",
        }
        url = f"{TIKTOK_SHOP_BASE}{path}"
        if method == "GET":
            resp = requests.get(url, headers=headers, params=signed_params, timeout=15)
        else:
            resp = requests.post(url, headers=headers, params=signed_params, data=body_str, timeout=15)
        if not resp.ok:
            raise TikTokError(f"TikTok Shop API 오류 (HTTP {resp.status_code}): {resp.text}")
        data = resp.json()
        if data.get("code") != 0:
            raise TikTokError(f"TikTok Shop 오류 (code {data.get('code')}): {data.get('message')}")
        return data.get("data") or {}

    def get_shop_orders(self, days_back: int = 1) -> list[dict]:
        if not (self.app_key and self.app_secret and self.shop_cipher):
            return []
        since = int((datetime.now(timezone.utc) - timedelta(days=days_back)).timestamp())
        data = self._shop_call("POST", "/order/202309/orders/search", body_data={
            "order_status": "AWAITING_SHIPMENT",
            "create_time_ge": since,
            "page_size": 20,
            "sort_field": "CREATE_TIME",
            "sort_order": "DESC",
        })
        return data.get("orders") or []

    def sync_products_to_shop(self, products: list[dict]) -> list[dict]:
        self._require_shop_creds()
        results = []
        for product in products:
            try:
                result = self._upsert_shop_product(product)
                results.append({"product_id": product.get("id"), "status": "ok", "result": result})
            except TikTokError as e:
                results.append({"product_id": product.get("id"), "status": "error", "error": str(e)})
        return results

    def _upsert_shop_product(self, product: dict) -> dict:
        variant = (product.get("variants") or [{}])[0]
        # 이미지는 TikTok 미디어 업로드 URI가 필요함 — Shopify CDN URL 직접 사용 불가
        # 실제 운영 시 /product/202309/images/upload로 사전 업로드 필요
        images = [{"uri": img["src"]} for img in (product.get("images") or []) if img.get("src")]
        body_data = {
            "title": product.get("title", ""),
            "description": re.sub(r"<[^>]+>", "", product.get("body_html", "") or product.get("title", "")),
            "category_id": "600001",  # 실제 카테고리 ID로 교체 필요
            "brand_id": "0",
            "main_images": images[:9],
            "skus": [
                {
                    "sales_attributes": [],
                    "seller_sku": str(variant.get("sku") or variant.get("id") or ""),
                    "price": {
                        "amount": str(variant.get("price") or "0"),
                        "currency": "SGD",
                    },
                    "stock_infos": [{"available_stock": variant.get("inventory_quantity") or 0}],
                }
            ],
            "package_weight": {"unit": "KILOGRAM", "value": "0.5"},
        }
        return self._shop_call("POST", "/product/202309/products", body_data=body_data)


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
