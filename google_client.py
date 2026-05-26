import json
import re
import time
import requests


GA4_COLLECT_URL = "https://www.google-analytics.com/mp/collect"
GA4_DEBUG_URL = "https://www.google-analytics.com/debug/mp/collect"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
MERCHANT_BASE = "https://shoppingcontent.googleapis.com/content/v2.1"


class GoogleClient:
    def __init__(
        self,
        ga4_measurement_id: str | None = None,
        ga4_api_secret: str | None = None,
        merchant_id: str | None = None,
        service_account_json: str | None = None,
    ):
        self.ga4_measurement_id = ga4_measurement_id
        self.ga4_api_secret = ga4_api_secret
        self.merchant_id = merchant_id
        self._sa_info = json.loads(service_account_json) if service_account_json else None
        self._access_token: str | None = None
        self._token_expires_at = 0.0

    # ── GA4 Measurement Protocol ───────────────────────────────────────────────

    def track_purchase(self, order: dict, debug: bool = False) -> dict:
        if not (self.ga4_measurement_id and self.ga4_api_secret):
            raise GoogleError("GA4 자격증명(GOOGLE_GA4_MEASUREMENT_ID / GOOGLE_GA4_API_SECRET)이 설정되지 않았습니다.")

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
                "item_brand": (i.get("vendor") or ""),
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
                        "transaction_id": order_number,
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

        url = GA4_DEBUG_URL if debug else GA4_COLLECT_URL
        resp = requests.post(
            url,
            params={"measurement_id": self.ga4_measurement_id, "api_secret": self.ga4_api_secret},
            json=payload,
            timeout=10,
        )
        resp.raise_for_status()
        if debug:
            return resp.json()
        return {"status": "ok", "order_number": order_number}

    # ── Google Merchant Center ─────────────────────────────────────────────────

    def _get_access_token(self) -> str:
        if self._access_token and time.time() < self._token_expires_at:
            return self._access_token
        if not self._sa_info:
            raise GoogleError("서비스 계정 JSON(GOOGLE_SERVICE_ACCOUNT_JSON)이 설정되지 않았습니다.")

        import base64
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding as asym_padding

        now = int(time.time())
        header = {"alg": "RS256", "typ": "JWT"}
        claim = {
            "iss": self._sa_info["client_email"],
            "scope": "https://www.googleapis.com/auth/content",
            "aud": GOOGLE_TOKEN_URL,
            "iat": now,
            "exp": now + 3600,
        }

        def _b64(data: dict) -> str:
            return base64.urlsafe_b64encode(json.dumps(data, separators=(",", ":")).encode()).rstrip(b"=").decode()

        signing_input = f"{_b64(header)}.{_b64(claim)}".encode()
        private_key = serialization.load_pem_private_key(
            self._sa_info["private_key"].encode(), password=None
        )
        signature = private_key.sign(signing_input, asym_padding.PKCS1v15(), hashes.SHA256())
        jwt_token = f"{signing_input.decode()}.{base64.urlsafe_b64encode(signature).rstrip(b'=').decode()}"

        resp = requests.post(
            GOOGLE_TOKEN_URL,
            data={"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": jwt_token},
            timeout=10,
        )
        if not resp.ok:
            raise GoogleError(f"Google 인증 실패: {resp.text}")
        data = resp.json()
        self._access_token = data["access_token"]
        self._token_expires_at = time.time() + data.get("expires_in", 3600) - 60
        return self._access_token

    def _merchant_headers(self) -> dict:
        return {"Authorization": f"Bearer {self._get_access_token()}", "Content-Type": "application/json"}

    def sync_products(self, shopify_products: list[dict], store_url: str = "", country: str = "SG") -> list[dict]:
        if not self.merchant_id:
            raise GoogleError("GOOGLE_MERCHANT_ID가 설정되지 않았습니다.")
        results = []
        for product in shopify_products:
            for variant in product.get("variants") or [{}]:
                try:
                    self._upsert_product(product, variant, store_url, country)
                    results.append({"product_id": product.get("id"), "variant_id": variant.get("id"), "status": "ok"})
                except GoogleError as e:
                    results.append({"product_id": product.get("id"), "variant_id": variant.get("id"), "status": "error", "error": str(e)})
        return results

    def _upsert_product(self, product: dict, variant: dict, store_url: str, country: str) -> dict:
        offer_id = f"shopify_{variant.get('id') or product.get('id')}"
        image = ((product.get("images") or [{}])[0]).get("src", "")
        price = float(variant.get("price") or 0)
        currency = "SGD"
        in_stock = (variant.get("inventory_quantity") or 0) > 0

        payload = {
            "offerId": offer_id,
            "title": product.get("title", ""),
            "description": _strip_html(product.get("body_html") or product.get("title") or "")[:5000],
            "link": f"https://{store_url}/products/{product.get('handle', '')}",
            "imageLink": image,
            "contentLanguage": "en",
            "targetCountry": country,
            "feedLabel": country,  # v2.1 필수 필드
            "channel": "online",
            "availability": "in stock" if in_stock else "out of stock",
            "condition": "new",
            "price": {"value": f"{price:.2f}", "currency": currency},
            "brand": product.get("vendor", ""),
            "mpn": str(variant.get("sku") or variant.get("id") or ""),
            "shipping": [
                {
                    "country": country,
                    "service": "Standard Shipping",
                    "price": {"value": "0.00", "currency": currency},
                }
            ],
        }

        resp = requests.post(
            f"{MERCHANT_BASE}/{self.merchant_id}/products",
            headers=self._merchant_headers(),
            json=payload,
            timeout=15,
        )
        if not resp.ok:
            raise GoogleError(f"Merchant Center 상품 등록 실패 (HTTP {resp.status_code}): {resp.text}")
        return resp.json()

    def generate_feed_xml(self, shopify_products: list[dict], store_url: str, country: str = "SG", currency: str = "SGD") -> str:
        lines = [
            '<?xml version="1.0" encoding="UTF-8"?>',
            '<rss version="2.0" xmlns:g="http://base.google.com/ns/1.0">',
            "<channel>",
            f"<title>{_xml_escape(store_url)}</title>",
            f"<link>https://{store_url}</link>",
            "<description>Google Shopping Feed</description>",
        ]
        for product in shopify_products:
            for variant in product.get("variants") or [{}]:
                offer_id = f"shopify_{variant.get('id') or product.get('id')}"
                image = ((product.get("images") or [{}])[0]).get("src", "")
                price = float(variant.get("price") or 0)
                in_stock = (variant.get("inventory_quantity") or 0) > 0
                title = _xml_escape(product.get("title", ""))
                desc = _xml_escape(_strip_html(product.get("body_html") or product.get("title") or "")[:5000])
                link = f"https://{store_url}/products/{product.get('handle', '')}"
                sku = str(variant.get("sku") or "")
                vendor = _xml_escape(product.get("vendor", ""))
                lines += [
                    "<item>",
                    f"  <g:id>{_xml_escape(offer_id)}</g:id>",
                    f"  <g:title>{title}</g:title>",
                    f"  <g:description>{desc}</g:description>",
                    f"  <g:link>{_xml_escape(link)}</g:link>",
                    f"  <g:image_link>{_xml_escape(image)}</g:image_link>",
                    f"  <g:availability>{'in stock' if in_stock else 'out of stock'}</g:availability>",
                    f"  <g:price>{price:.2f} {currency}</g:price>",
                    f"  <g:condition>new</g:condition>",
                    f"  <g:brand>{vendor}</g:brand>",
                    f"  <g:mpn>{_xml_escape(sku)}</g:mpn>",
                    f"  <g:shipping><g:country>{country}</g:country><g:service>Standard</g:service><g:price>0.00 {currency}</g:price></g:shipping>",
                    "</item>",
                ]
        lines += ["</channel>", "</rss>"]
        return "\n".join(lines)


class GoogleError(Exception):
    pass


def _stable_client_id(seed: str) -> str:
    import hashlib
    h = hashlib.md5(str(seed).encode()).hexdigest()
    return f"{h[:8]}.{h[8:16]}"


def _strip_html(html: str) -> str:
    return re.sub(r"<[^>]+>", "", html).strip()


def _xml_escape(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
