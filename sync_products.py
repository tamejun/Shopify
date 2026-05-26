"""
Shopify 상품 카탈로그를 TikTok Shop 및 Google Merchant Center에 동기화.

사용법:
    python sync_products.py              # TikTok + Google 모두 동기화
    python sync_products.py --tiktok     # TikTok만
    python sync_products.py --google     # Google만
    python sync_products.py --feed       # Google Shopping XML 피드 파일 생성
"""

import os
import sys
import logging
import argparse
from dotenv import load_dotenv

from shopify_client import ShopifyClient, ShopifyError
from tiktok_client import TikTokClient, TikTokError
from google_client import GoogleClient, GoogleError

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
log = logging.getLogger(__name__)


def fetch_all_products(shopify: ShopifyClient) -> list[dict]:
    products = []
    url = f"{shopify.base_url}/products.json"
    params = {"limit": 250, "status": "active"}
    while url:
        resp = shopify._get(url, params=params)
        batch = resp.json().get("products", [])
        products.extend(batch)
        link = resp.headers.get("Link", "")
        url = None
        params = None
        if 'rel="next"' in link:
            for part in link.split(","):
                if 'rel="next"' in part:
                    url = part.split(";")[0].strip().strip("<>")
                    break
    return products


def sync_to_tiktok(products: list[dict], cfg: dict):
    tt = cfg["tiktok"]
    if not (tt["access_token"] and tt["app_key"] and tt["app_secret"]):
        log.warning("TikTok Shop 자격증명 미설정 (TIKTOK_ACCESS_TOKEN, TIKTOK_SHOP_APP_KEY, TIKTOK_SHOP_APP_SECRET)")
        return

    client = TikTokClient(tt["access_token"], tt.get("pixel_id", ""), tt["app_key"], tt["app_secret"])
    log.info(f"TikTok Shop 상품 동기화 시작 ({len(products)}개)...")
    results = client.sync_products_to_shop(products)

    ok = sum(1 for r in results if r["status"] == "ok")
    err = sum(1 for r in results if r["status"] == "error")
    for r in results:
        if r["status"] == "error":
            log.error(f"  상품 {r.get('product_id')}: {r['error']}")
    log.info(f"TikTok 동기화 완료: 성공 {ok}건 / 실패 {err}건")


def sync_to_google(products: list[dict], cfg: dict):
    g = cfg["google"]
    if not (g["merchant_id"] and g["service_account_json"]):
        log.warning("Google Merchant Center 자격증명 미설정 (GOOGLE_MERCHANT_ID, GOOGLE_SERVICE_ACCOUNT_JSON)")
        return

    client = GoogleClient(
        merchant_id=g["merchant_id"],
        service_account_json=g["service_account_json"],
    )
    log.info(f"Google Merchant Center 상품 동기화 시작 ({len(products)}개)...")
    results = client.sync_products(products)

    ok = sum(1 for r in results if r["status"] == "ok")
    err = sum(1 for r in results if r["status"] == "error")
    for r in results:
        if r["status"] == "error":
            log.error(f"  상품 {r.get('product_id')} 변형 {r.get('variant_id')}: {r['error']}")
    log.info(f"Google Merchant Center 동기화 완료: 성공 {ok}건 / 실패 {err}건")


def generate_feed(products: list[dict], cfg: dict):
    g = cfg["google"]
    store_url = cfg["shopify_store_url"]
    client = GoogleClient()
    feed_xml = client.generate_feed_xml(products, store_url)
    filename = "google_shopping_feed.xml"
    with open(filename, "w", encoding="utf-8") as f:
        f.write(feed_xml)
    log.info(f"Google Shopping XML 피드 생성 완료: {filename} ({len(products)}개 상품)")
    log.info("  → Google Merchant Center → 피드 → 파일 업로드에서 이 파일을 사용하세요.")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--tiktok", action="store_true", help="TikTok Shop만 동기화")
    parser.add_argument("--google", action="store_true", help="Google Merchant Center만 동기화")
    parser.add_argument("--feed", action="store_true", help="Google Shopping XML 피드 파일 생성")
    args = parser.parse_args()

    run_tiktok = args.tiktok or (not args.tiktok and not args.google and not args.feed)
    run_google = args.google or (not args.tiktok and not args.google and not args.feed)
    run_feed = args.feed

    required = ["SHOPIFY_STORE_URL", "SHOPIFY_ACCESS_TOKEN"]
    missing = [k for k in required if not os.getenv(k)]
    if missing:
        log.error(f".env에 누락된 환경변수: {', '.join(missing)}")
        sys.exit(1)

    cfg = {
        "shopify_store_url": os.environ["SHOPIFY_STORE_URL"],
        "shopify_access_token": os.environ["SHOPIFY_ACCESS_TOKEN"],
        "tiktok": {
            "access_token": os.getenv("TIKTOK_ACCESS_TOKEN", ""),
            "pixel_id": os.getenv("TIKTOK_PIXEL_ID", ""),
            "app_key": os.getenv("TIKTOK_SHOP_APP_KEY", ""),
            "app_secret": os.getenv("TIKTOK_SHOP_APP_SECRET", ""),
        },
        "google": {
            "ga4_measurement_id": os.getenv("GOOGLE_GA4_MEASUREMENT_ID", ""),
            "ga4_api_secret": os.getenv("GOOGLE_GA4_API_SECRET", ""),
            "merchant_id": os.getenv("GOOGLE_MERCHANT_ID", ""),
            "service_account_json": os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", ""),
        },
    }

    shopify = ShopifyClient(cfg["shopify_store_url"], cfg["shopify_access_token"])

    log.info("Shopify 상품 목록 조회 중...")
    products = fetch_all_products(shopify)
    log.info(f"상품 {len(products)}개 조회 완료.")

    if not products:
        log.info("동기화할 상품이 없습니다.")
        return

    if run_tiktok:
        sync_to_tiktok(products, cfg)
    if run_google:
        sync_to_google(products, cfg)
    if run_feed:
        generate_feed(products, cfg)


if __name__ == "__main__":
    main()
