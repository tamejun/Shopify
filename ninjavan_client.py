import re
import requests
from datetime import datetime, timedelta

NINJAVAN_BASE_URL = "https://api.ninjavan.co/{country}"
PICKUP_CUTOFF_HOUR = 14


class NinjaVanClient:
    def __init__(self, client_id: str, client_secret: str, country: str):
        self.country = country.upper()
        self.base_url = NINJAVAN_BASE_URL.format(country=self.country)
        self.client_id = client_id
        self.client_secret = client_secret
        self._token = None

    def _get_token(self) -> str:
        if self._token:
            return self._token
        resp = requests.post(f"{self.base_url}/2.0/oauth/access_token", json={
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "grant_type": "client_credentials",
        })
        if not resp.ok:
            raise NinjaVanError(f"NinjaVan 인증 실패 (HTTP {resp.status_code}): {resp.text}")
        self._token = resp.json()["access_token"]
        return self._token

    def _headers(self):
        return {"Authorization": f"Bearer {self._get_token()}", "Content-Type": "application/json"}

    def get_order(self, merchant_order_number: str) -> dict | None:
        resp = requests.get(f"{self.base_url}/2.0/orders/{merchant_order_number}", headers=self._headers())
        if resp.ok:
            return resp.json()
        return None

    def create_order(self, order: dict, sender: dict, pickup_date: str | None = None) -> dict:
        shipping_addr = order.get("shipping_address") or order.get("billing_address") or {}
        customer = order.get("customer") or {}
        items = order.get("line_items") or []
        order_number = order.get("order_number")

        to_phone = _first_nonempty(
            shipping_addr.get("phone"),
            (order.get("billing_address") or {}).get("phone"),
            customer.get("phone"),
        )
        to_name = _resolve_name(shipping_addr)

        missing = []
        if not to_name:            missing.append("수신자 이름")
        if not shipping_addr.get("address1", "").strip():
            missing.append("배송 주소(address1)")
        if not shipping_addr.get("city", "").strip():
            missing.append("도시(city)")
        if not to_phone:
            missing.append("수신자 전화번호")
        if missing:
            raise NinjaVanError(f"주문 #{order_number} 필수 정보 누락: {', '.join(missing)}")

        pickup_date = pickup_date or _next_business_day()
        delivery_date = _next_business_day(after=datetime.strptime(pickup_date, "%Y-%m-%d") + timedelta(days=1))

        to_contact = {"name": to_name, "phone_number": _normalize_phone(to_phone, self.country), "address": _build_to_address(shipping_addr, self.country)}
        if customer.get("email"):
            to_contact["email"] = customer["email"]

        from_contact = {"name": sender["name"], "phone_number": _normalize_phone(sender["phone"], self.country), "address": _build_from_address(sender, self.country)}
        if sender.get("email"):
            from_contact["email"] = sender["email"]

        payload = {
            "service_type": "Parcel",
            "service_level": "Standard",
            "reference": {"merchant_order_number": str(order_number)},
            "from": from_contact,
            "to": to_contact,
            "parcel_job": {
                "is_pickup_required": True,
                "pickup_service_type": "Scheduled",
                "pickup_service_level": "Standard",
                "pickup_date": pickup_date,
                "pickup_timeslot": {"start_time": "09:00", "end_time": "12:00", "timezone": _country_timezone(self.country)},
                "delivery_start_date": delivery_date,
                "delivery_timeslot": {"start_time": "09:00", "end_time": "22:00", "timezone": _country_timezone(self.country)},
                "dimensions": {"weight": _total_weight_kg(items)},
                "items": _build_items(items),
            },
        }

        resp = requests.post(f"{self.base_url}/2.0/orders", headers=self._headers(), json=payload)

        if resp.status_code == 409:
            try:
                data = resp.json()
                tracking = data.get("tracking_number") or (data.get("data") or {}).get("tracking_number", "")
                if tracking:
                    return {"tracking_number": tracking, "already_registered": True}
            except Exception:
                pass
            raise NinjaVanError(f"주문 #{order_number}: 이미 NinjaVan에 등록된 주문번호입니다.")

        if not resp.ok:
            try:
                err_body = resp.json()
            except Exception:
                err_body = resp.text
            raise NinjaVanError(f"NinjaVan API 오류 (HTTP {resp.status_code}): {err_body}")

        return resp.json()

    def get_tracking_url(self, tracking_number: str) -> str:
        return f"https://www.ninjavan.co/tracking/{tracking_number}"


class NinjaVanError(Exception):
    pass


def _first_nonempty(*values) -> str:
    for v in values:
        if v and str(v).strip():
            return str(v).strip()
    return ""


def _resolve_name(addr: dict) -> str:
    name = addr.get("name", "").strip()
    if name:
        return name
    return f"{addr.get('first_name','').strip()} {addr.get('last_name','').strip()}".strip()


def _build_to_address(addr: dict, default_country: str) -> dict:
    country = (addr.get("country_code") or default_country).upper()
    state = (addr.get("province") or addr.get("province_code") or "").strip()
    if not state and country in ("SG", "BN"):
        state = {"SG": "Singapore", "BN": "Brunei"}.get(country, "")
    result = {
        "address1": addr.get("address1", "").strip(),
        "city": addr.get("city", "").strip(),
        "state": state,
        "postcode": re.sub(r"\s+", "", (addr.get("zip") or "").strip()),
        "country": country,
    }
    if addr.get("address2", "").strip():
        result["address2"] = addr["address2"].strip()
    return result


def _build_from_address(sender: dict, country: str) -> dict:
    result = {
        "address1": sender["address1"].strip(),
        "city": sender["city"].strip(),
        "state": sender["state"].strip(),
        "postcode": re.sub(r"\s+", "", (sender.get("postcode") or "").strip()),
        "country": country.upper(),
    }
    if sender.get("address2", "").strip():
        result["address2"] = sender["address2"].strip()
    return result


def _normalize_phone(phone: str, country: str) -> str:
    if not phone:
        return ""
    cleaned = re.sub(r"[^\d+]", "", phone.strip())
    if cleaned.startswith("+"):
        return cleaned
    if cleaned.startswith("00"):
        return "+" + cleaned[2:]
    codes = {"MY": "+60", "SG": "+65", "PH": "+63", "ID": "+62", "TH": "+66", "VN": "+84"}
    prefix = codes.get(country, "")
    digits = prefix.lstrip("+")
    if prefix and digits and cleaned.startswith(digits):
        return "+" + cleaned
    if prefix and cleaned.startswith("0"):
        return prefix + cleaned[1:]
    if prefix and cleaned:
        return prefix + cleaned
    return cleaned


def _total_weight_kg(items: list) -> float:
    total = sum((i.get("grams") or 0) * (i.get("quantity") or 1) for i in items)
    return max(round(total / 1000, 2), 0.1)


def _build_items(items: list) -> list:
    if not items:
        return [{"item_description": "Parcel", "quantity": 1, "is_dangerous_good": False}]
    return [{"item_description": (i.get("name") or "Item")[:255], "quantity": max(i.get("quantity") or 1, 1), "is_dangerous_good": False} for i in items]


def _next_business_day(after=None) -> str:
    if after is None:
        now = datetime.now()
        base = now.date() if now.hour < PICKUP_CUTOFF_HOUR else (now + timedelta(days=1)).date()
    else:
        base = after.date() if hasattr(after, "date") else after
    while base.weekday() >= 5:
        base += timedelta(days=1)
    return base.strftime("%Y-%m-%d")


def _country_timezone(country: str) -> str:
    return {"SG": "Asia/Singapore", "MY": "Asia/Kuala_Lumpur", "PH": "Asia/Manila", "ID": "Asia/Jakarta", "TH": "Asia/Bangkok", "VN": "Asia/Ho_Chi_Minh"}.get(country, "Asia/Singapore")
