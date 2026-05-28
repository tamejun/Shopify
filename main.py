"""
오늘 Shopify 주문을 NinjaVan에 자동으로 송장 등록하는 스크립트.
TikTok Events API 및 Google Analytics 4 구매 이벤트 서버 전송 포함.

사용법:
    pip install -r requirements.txt
    cp .env.example .env   # .env 파일에 실제 값 입력
    python main.py
"""

import os
import sys
import logging
from dotenv import load_dotenv

from shopify_client import ShopifyClient, ShopifyError
from ninjavan_client import NinjaVanClient, NinjaVanError
from tiktok_client import TikTokClient, TikTokError
from google_client import GoogleClient, GoogleError

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
log = logging.getLogger(__name__)


def load_config() -> dict:
    required = [
        "SHOPIFY_STORE_URL", "SHOPIFY_ACCESS_TOKEN",
        "NINJAVAN_CLIENT_ID", "NINJAVAN_CLIENT_SECRET", "NINJAVAN_COUNTRY",
        "SENDER_NAME", "SENDER_PHONE", "SENDER_ADDRESS1", "SENDER_CITY", "SENDER_STATE",
    ]
    missing = [k for k in required if not os.getenv(k)]
    if missing:
        log.error(f".env에 누락된 환경변수: {', '.join(missing)}")
        sys.exit(1)

    return {
        "shopify_store_url": os.environ["SHOPIFY_STORE_URL"],
        "shopify_access_token": os.environ["SHOPIFY_ACCESS_TOKEN"],
        "ninjavan_client_id": os.environ["NINJAVAN_CLIENT_ID"],
        "ninjavan_client_secret": os.environ["NINJAVAN_CLIENT_SECRET"],
        "ninjavan_country": os.environ["NINJAVAN_COUNTRY"],
        "sender": {
            "name": os.environ["SENDER_NAME"],
            "phone": os.environ["SENDER_PHONE"],
            "email": os.getenv("SENDER_EMAIL", ""),
            "address1": os.environ["SENDER_ADDRESS1"],
            "address2": os.getenv("SENDER_ADDRESS2", ""),
            "city": os.environ["SENDER_CITY"],
            "state": os.environ["SENDER_STATE"],
            "postcode": os.getenv("SENDER_POSTCODE", ""),
        },
        "tiktok_access_token": os.getenv("TIKTOK_ACCESS_TOKEN", ""),
        "tiktok_pixel_id": os.getenv("TIKTOK_PIXEL_ID", ""),
        "ga4_measurement_id": os.getenv("GOOGLE_GA4_MEASUREMENT_ID", ""),
        "ga4_api_secret": os.getenv("GOOGLE_GA4_API_SECRET", ""),
    }


def main():
    cfg = load_config()
    shopify = ShopifyClient(cfg["shopify_store_url"], cfg["shopify_access_token"])
    ninjavan = NinjaVanClient(cfg["ninjavan_client_id"], cfg["ninjavan_client_secret"], cfg["ninjavan_country"])

    tiktok = None
    if cfg["tiktok_access_token"] and cfg["tiktok_pixel_id"]:
        tiktok = TikTokClient(cfg["tiktok_access_token"], cfg["tiktok_pixel_id"])
        log.info("TikTok Events API 연동 활성화")

    google = None
    if cfg["ga4_measurement_id"] and cfg["ga4_api_secret"]:
        google = GoogleClient(cfg["ga4_measurement_id"], cfg["ga4_api_secret"])
        log.info("Google Analytics 4 연동 활성화")

    log.info("오늘 미처리 주문 조회 중...")
    orders = shopify.get_todays_orders_without_tracking()

    if not orders:
        log.info("오늘 등록할 주문이 없습니다.")
        return

    log.info(f"총 {len(orders)}건 주문 발견.")
    success, skipped, failed = 0, 0, 0

    for order in orders:
        order_number = order.get("order_number")
        order_id = order["id"]

        log.info(f"  주문 #{order_number} NinjaVan 송장 등록 중...")
        try:
            nv_resp = ninjavan.create_order(order, cfg["sender"])
            tracking_number = nv_resp.get("tracking_number") or (nv_resp.get("data") or {}).get("tracking_number", "")

            if not tracking_number:
                log.warning(f"  주문 #{order_number}: tracking_number 없음. 응답: {nv_resp}")
                failed += 1
                continue

            already = nv_resp.get("already_registered")
            log.info(f"  주문 #{order_number}: {'이미 등록됨(재사용)' if already else '등록 완료'} → {tracking_number}")

            if tiktok:
                try:
                    tiktok.track_purchase(order)
                    log.info(f"  주문 #{order_number}: TikTok 이벤트 전송 완료")
                except (TikTokError, Exception) as e:
                    log.warning(f"  주문 #{order_number}: TikTok 이벤트 전송 실패 (비필수) - {e}")

            if google:
                try:
                    google.track_purchase(order)
                    log.info(f"  주문 #{order_number}: GA4 이벤트 전송 완료")
                except (GoogleError, Exception) as e:
                    log.warning(f"  주문 #{order_number}: GA4 이벤트 전송 실패 (비필수) - {e}")

            try:
                shopify.update_fulfillment(order_id, tracking_number, ninjavan.get_tracking_url(tracking_number))
                log.info(f"  주문 #{order_number}: Shopify 업데이트 완료")
                success += 1
            except ShopifyError as e:
                log.error(f"  주문 #{order_number}: Shopify 업데이트 실패 - {e}\n  >> 운송장 번호 {tracking_number} 를 Shopify에 수동 입력하세요.")
                failed += 1

        except NinjaVanError as e:
            log.error(f"  주문 #{order_number}: NinjaVan 오류 - {e}")
            failed += 1
        except Exception as e:
            log.error(f"  주문 #{order_number}: 오류 - {e}")
            failed += 1

    log.info(f"\n결과: 성공 {success}건 / 건너뜀 {skipped}건 / 실패 {failed}건 (전체 {len(orders)}건)")


if __name__ == "__main__":
    main()
