import time
import requests
from datetime import datetime, timezone


class ShopifyClient:
    def __init__(self, store_url: str, access_token: str):
        self.base_url = f"https://{store_url}/admin/api/2024-01"
        self.headers = {
            "X-Shopify-Access-Token": access_token,
            "Content-Type": "application/json",
        }

    def get_todays_orders_without_tracking(self) -> list[dict]:
        now = datetime.now(timezone.utc)
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        created_at_min = today_start.strftime("%Y-%m-%dT%H:%M:%S%z")

        orders = []
        url = f"{self.base_url}/orders.json"
        params = {"status": "any", "created_at_min": created_at_min, "limit": 250}

        while url:
            resp = self._get(url, params=params)
            orders.extend(resp.json().get("orders", []))
            link = resp.headers.get("Link", "")
            url = None
            params = None
            if 'rel="next"' in link:
                for part in link.split(","):
                    if 'rel="next"' in part:
                        url = part.split(";")[0].strip().strip("<>")
                        break

        result = []
        for order in orders:
            if order.get("cancelled_at"):
                continue
            has_tracking = any(f.get("tracking_number") for f in order.get("fulfillments", []))
            if not has_tracking:
                result.append(order)
        return result

    def update_fulfillment(self, order_id: int, tracking_number: str, tracking_url: str = "") -> dict:
        fo_resp = self._get(f"{self.base_url}/orders/{order_id}/fulfillment_orders.json")
        fulfillment_orders = fo_resp.json().get("fulfillment_orders", [])
        open_fos = [fo for fo in fulfillment_orders if fo.get("status") in ("open", "in_progress")]

        tracking_info = {"number": tracking_number, "url": tracking_url, "company": "Ninja Van"}

        if open_fos:
            payload = {
                "fulfillment": {
                    "line_items_by_fulfillment_order": [{"fulfillment_order_id": fo["id"]} for fo in open_fos],
                    "tracking_info": tracking_info,
                    "notify_customer": True,
                }
            }
            resp = self._post(f"{self.base_url}/fulfillments.json", payload)
            return resp.json()

        f_resp = self._get(f"{self.base_url}/orders/{order_id}/fulfillments.json")
        fulfillments = f_resp.json().get("fulfillments", [])
        if not fulfillments:
            raise ShopifyError(f"주문 {order_id}: fulfillment를 찾을 수 없습니다.")

        target = next((f for f in fulfillments if not f.get("tracking_number")), fulfillments[0])
        payload = {"fulfillment": {"tracking_info": tracking_info, "notify_customer": True}}
        resp = self._post(f"{self.base_url}/fulfillments/{target['id']}/update_tracking.json", payload)
        return resp.json()

    def _get(self, url, params=None):
        return self._request("GET", url, params=params)

    def _post(self, url, json):
        return self._request("POST", url, json=json)

    def _request(self, method, url, **kwargs):
        for attempt in range(4):
            resp = requests.request(method, url, headers=self.headers, **kwargs)
            if resp.status_code == 429:
                time.sleep(float(resp.headers.get("Retry-After", 2 ** attempt)))
                continue
            if not resp.ok:
                raise ShopifyError(f"Shopify API 오류 (HTTP {resp.status_code}): {resp.text}")
            return resp
        raise ShopifyError(f"Shopify API 레이트 리밋 초과: {url}")


class ShopifyError(Exception):
    pass
