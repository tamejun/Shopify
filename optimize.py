"""
Shopify 상품 노출 최적화 — Google / TikTok 채널 대응.

사용법:
    python optimize.py audit      # 진단 보고서 (HTML)
    python optimize.py suggest    # 제목 키워드 개선 제안 (CSV)
    python optimize.py fix        # 자동 수정 (미리보기 → 확인 → 적용)
"""

import os
import re
import sys
import csv
import html
import argparse
import logging
import webbrowser
from datetime import datetime
from collections import Counter
from dotenv import load_dotenv

from shopify_client import ShopifyClient, ShopifyError

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger(__name__)


# ─── 진단 (Audit) ─────────────────────────────────────────────────────────────

CHECKS = [
    ("title_length",       "제목이 너무 짧음 (20자 미만)",      10, "google"),
    ("title_too_long",     "제목이 너무 김 (150자 초과)",       5,  "google"),
    ("description_short",  "상품 설명이 너무 짧음 (100자 미만)", 15, "both"),
    ("no_vendor",          "브랜드명(Vendor) 없음",             15, "google"),
    ("no_product_type",    "상품 유형(Type) 없음",              10, "google"),
    ("no_image",           "이미지 없음",                       30, "both"),
    ("few_images",         "이미지 1장뿐 (3장 이상 권장)",      10, "both"),
    ("image_no_alt",       "이미지 alt 텍스트 누락",            5,  "google"),
    ("no_barcode",         "바코드(GTIN) 누락 변형",            10, "google"),
    ("no_tags",            "태그 없음",                         5,  "both"),
    ("all_oos",            "모든 변형 재고 없음",               20, "both"),
    ("title_no_keywords",  "제목에 검색 키워드 부족",           10, "tiktok"),
]


def audit_product(product: dict) -> dict:
    issues = []
    score = 100

    title = product.get("title", "") or ""
    body_clean = re.sub(r"<[^>]+>", "", product.get("body_html") or "").strip()
    vendor = (product.get("vendor") or "").strip()
    product_type = (product.get("product_type") or "").strip()
    tags = (product.get("tags") or "").strip()
    images = product.get("images") or []
    variants = product.get("variants") or []

    if len(title) < 20:
        issues.append(("제목이 너무 짧음 (20자 미만)", 10))
        score -= 10
    elif len(title) > 150:
        issues.append(("제목이 너무 김 (150자 초과)", 5))
        score -= 5

    if len(body_clean) < 100:
        issues.append(("상품 설명이 너무 짧음 (100자 미만)", 15))
        score -= 15

    if not vendor:
        issues.append(("브랜드명(Vendor) 없음", 15))
        score -= 15

    if not product_type:
        issues.append(("상품 유형(Type) 없음", 10))
        score -= 10

    if not images:
        issues.append(("이미지 없음", 30))
        score -= 30
    elif len(images) == 1:
        issues.append(("이미지 1장뿐 (3장 이상 권장)", 10))
        score -= 10

    images_no_alt = [img for img in images if not (img.get("alt") or "").strip()]
    if images_no_alt:
        issues.append((f"이미지 alt 텍스트 누락 ({len(images_no_alt)}장)", 5))
        score -= 5

    variants_no_barcode = [v for v in variants if not (v.get("barcode") or "").strip()]
    if variants_no_barcode:
        issues.append((f"바코드(GTIN) 누락 변형 ({len(variants_no_barcode)}개)", 10))
        score -= 10

    if not tags:
        issues.append(("태그 없음", 5))
        score -= 5

    if variants:
        in_stock = sum(1 for v in variants if (v.get("inventory_quantity") or 0) > 0)
        if in_stock == 0:
            issues.append(("모든 변형 재고 없음", 20))
            score -= 20

    if product_type and product_type.lower() not in title.lower():
        issues.append(("제목에 카테고리 키워드 없음 (검색 노출 약화)", 10))
        score -= 10

    return {
        "id": product.get("id"),
        "title": title,
        "handle": product.get("handle"),
        "score": max(score, 0),
        "issues": issues,
        "image_count": len(images),
        "variant_count": len(variants),
    }


def cmd_audit(shopify: ShopifyClient, store_url: str):
    log.info("Shopify 상품 조회 중...")
    products = shopify.get_all_products()
    log.info(f"총 {len(products)}개 상품 조회 완료.")

    if not products:
        log.warning("진단할 상품이 없습니다.")
        return

    results = [audit_product(p) for p in products]
    results.sort(key=lambda r: r["score"])

    avg = sum(r["score"] for r in results) / len(results)
    perfect = sum(1 for r in results if r["score"] >= 90)
    bad = sum(1 for r in results if r["score"] < 60)

    issue_counter = Counter()
    for r in results:
        for issue, _ in r["issues"]:
            issue_counter[re.sub(r"\(\d+[장개]\)", "", issue).strip()] += 1

    filename = f"audit_report_{datetime.now().strftime('%Y%m%d_%H%M')}.html"
    html_content = _render_html_report(results, avg, perfect, bad, issue_counter, store_url)
    with open(filename, "w", encoding="utf-8") as f:
        f.write(html_content)

    log.info(f"진단 완료. 평균 점수: {avg:.1f}/100")
    log.info(f"  ✅ 우수 (90점 이상): {perfect}개")
    log.info(f"  ⚠️  개선 필요 (60점 미만): {bad}개")
    log.info(f"보고서 파일: {filename}")

    try:
        webbrowser.open(f"file://{os.path.abspath(filename)}")
    except Exception:
        pass


def _render_html_report(results, avg, perfect, bad, issue_counter, store_url):
    rows = []
    for r in results:
        color = "#22c55e" if r["score"] >= 90 else "#f59e0b" if r["score"] >= 60 else "#ef4444"
        issues_html = "<br>".join(f"• {html.escape(i[0])}" for i in r["issues"]) or "<i>문제 없음</i>"
        edit_url = f"https://{store_url}/admin/products/{r['id']}"
        rows.append(f"""
        <tr>
            <td style="text-align:center"><span style="color:{color};font-weight:bold;font-size:18px">{r['score']}</span></td>
            <td><a href="{edit_url}" target="_blank">{html.escape(r['title'][:80])}</a></td>
            <td>{r['image_count']}장</td>
            <td>{r['variant_count']}개</td>
            <td style="font-size:13px">{issues_html}</td>
        </tr>""")

    top_issues = "".join(f"<tr><td>{html.escape(issue)}</td><td style='text-align:right'>{count}개</td></tr>"
                        for issue, count in issue_counter.most_common(10))

    total = len(results)
    return f"""<!DOCTYPE html>
<html lang="ko"><head><meta charset="UTF-8"><title>상품 진단 보고서</title>
<style>
body{{font-family:-apple-system,'Segoe UI',sans-serif;max-width:1200px;margin:20px auto;padding:20px;background:#f9fafb}}
h1{{color:#111827}}
.summary{{display:flex;gap:20px;margin:20px 0}}
.card{{background:white;padding:20px;border-radius:8px;flex:1;box-shadow:0 1px 3px rgba(0,0,0,0.1)}}
.card .num{{font-size:32px;font-weight:bold}}
.card .label{{color:#6b7280;font-size:14px}}
table{{width:100%;background:white;border-collapse:collapse;border-radius:8px;overflow:hidden;box-shadow:0 1px 3px rgba(0,0,0,0.1)}}
th{{background:#f3f4f6;padding:12px;text-align:left;font-size:14px;color:#374151}}
td{{padding:12px;border-top:1px solid #e5e7eb;vertical-align:top}}
tr:hover{{background:#f9fafb}}
a{{color:#2563eb;text-decoration:none}}
a:hover{{text-decoration:underline}}
.tag-good{{color:#22c55e}}.tag-warn{{color:#f59e0b}}.tag-bad{{color:#ef4444}}
.section{{margin-top:30px}}
.tip{{background:#eff6ff;border-left:4px solid #3b82f6;padding:12px;margin:20px 0;border-radius:4px}}
</style></head><body>

<h1>📊 상품 노출 최적화 진단</h1>
<p style="color:#6b7280">생성 시각: {datetime.now().strftime('%Y-%m-%d %H:%M')} · 대상: {total}개 상품</p>

<div class="summary">
    <div class="card"><div class="num">{avg:.1f}</div><div class="label">평균 점수</div></div>
    <div class="card"><div class="num tag-good">{perfect}</div><div class="label">우수 (90점 이상)</div></div>
    <div class="card"><div class="num tag-warn">{total - perfect - bad}</div><div class="label">보통 (60~89점)</div></div>
    <div class="card"><div class="num tag-bad">{bad}</div><div class="label">개선 필요 (60점 미만)</div></div>
</div>

<div class="tip">
💡 <strong>점수 해석:</strong> 90점 이상이면 Google·TikTok에 충실하게 노출됩니다.
60점 미만 상품은 우선적으로 개선하세요.<br>
<strong>다음 단계:</strong> <code>python optimize.py fix</code> 로 자동 수정 가능한 항목을 일괄 처리할 수 있습니다.
</div>

<div class="section">
<h2>🔥 가장 많이 발견된 문제</h2>
<table><thead><tr><th>문제</th><th style="text-align:right">발생 상품 수</th></tr></thead>
<tbody>{top_issues}</tbody></table>
</div>

<div class="section">
<h2>📋 상품별 상세 (점수 낮은 순)</h2>
<table><thead><tr>
<th style="width:80px;text-align:center">점수</th>
<th>상품명 (클릭 시 Shopify 편집)</th>
<th style="width:80px">이미지</th>
<th style="width:80px">변형</th>
<th>발견된 문제</th>
</tr></thead><tbody>
{''.join(rows)}
</tbody></table>
</div>

</body></html>"""


# ─── 자동 수정 (Fix) ──────────────────────────────────────────────────────────

def _plan_fixes(products: list[dict]) -> list[dict]:
    plans = []
    for p in products:
        title = p.get("title", "") or ""
        actions = []

        # 1. 이미지 alt 텍스트 자동 생성
        for img in p.get("images") or []:
            if not (img.get("alt") or "").strip():
                alt = title[:255]
                if alt:
                    actions.append({"type": "image_alt", "image_id": img["id"], "value": alt})

        # 2. SEO 메타 설명 자동 생성 (body_html에서 첫 160자)
        body_clean = re.sub(r"<[^>]+>", "", p.get("body_html") or "").strip()
        body_clean = re.sub(r"\s+", " ", body_clean)
        if body_clean and len(body_clean) >= 50:
            actions.append({
                "type": "meta_description",
                "value": body_clean[:160],
            })

        # 3. 상품 유형이 없으면 태그에서 추출 시도
        if not (p.get("product_type") or "").strip():
            tags = [t.strip() for t in (p.get("tags") or "").split(",") if t.strip()]
            if tags:
                actions.append({"type": "product_type", "value": tags[0]})

        if actions:
            plans.append({"product_id": p["id"], "title": title, "actions": actions})
    return plans


def cmd_fix(shopify: ShopifyClient):
    log.info("Shopify 상품 조회 중...")
    products = shopify.get_all_products()
    log.info(f"총 {len(products)}개 상품 조회 완료.")

    plans = _plan_fixes(products)

    if not plans:
        log.info("자동 수정할 항목이 없습니다. 이미 잘 채워져 있습니다 ✅")
        return

    # 미리보기 출력
    alt_count = sum(1 for plan in plans for a in plan["actions"] if a["type"] == "image_alt")
    meta_count = sum(1 for plan in plans for a in plan["actions"] if a["type"] == "meta_description")
    type_count = sum(1 for plan in plans for a in plan["actions"] if a["type"] == "product_type")

    print("\n" + "=" * 60)
    print("📝 자동 수정 미리보기")
    print("=" * 60)
    print(f"  • 이미지 alt 텍스트 추가:    {alt_count}장")
    print(f"  • SEO 메타 설명 추가:        {meta_count}개 상품")
    print(f"  • 상품 유형(Type) 추가:      {type_count}개 상품")
    print("=" * 60)

    # 처음 5개 상품 샘플 출력
    print("\n예시 (처음 5개):")
    for plan in plans[:5]:
        print(f"\n  📦 {plan['title'][:60]}")
        for a in plan["actions"][:3]:
            label = {"image_alt": "이미지 alt", "meta_description": "SEO 설명", "product_type": "상품 유형"}.get(a["type"], a["type"])
            print(f"     → {label}: {str(a['value'])[:80]}")

    print("\n" + "=" * 60)
    answer = input("이대로 적용하시겠습니까? (y/n): ").strip().lower()
    if answer != "y":
        log.info("취소되었습니다.")
        return

    log.info("적용 시작...")
    ok, err = 0, 0
    for plan in plans:
        pid = plan["product_id"]
        # 상품 필드 업데이트 (meta_description, product_type)
        update_fields = {}
        for a in plan["actions"]:
            if a["type"] == "meta_description":
                update_fields["metafields_global_description_tag"] = a["value"]
            elif a["type"] == "product_type":
                update_fields["product_type"] = a["value"]

        if update_fields:
            try:
                shopify.update_product(pid, update_fields)
                ok += 1
            except ShopifyError as e:
                log.error(f"상품 {pid} 업데이트 실패: {e}")
                err += 1

        # 이미지 alt 업데이트
        for a in plan["actions"]:
            if a["type"] == "image_alt":
                try:
                    shopify.update_image_alt(pid, a["image_id"], a["value"])
                    ok += 1
                except ShopifyError as e:
                    log.error(f"상품 {pid} 이미지 {a['image_id']} 실패: {e}")
                    err += 1

    log.info(f"적용 완료: 성공 {ok}건 / 실패 {err}건")


# ─── 제목 키워드 제안 (Suggest) ──────────────────────────────────────────────

def cmd_suggest(shopify: ShopifyClient):
    log.info("Shopify 상품 조회 중...")
    products = shopify.get_all_products()

    suggestions = []
    for p in products:
        title = (p.get("title") or "").strip()
        product_type = (p.get("product_type") or "").strip()
        vendor = (p.get("vendor") or "").strip()
        tags = [t.strip() for t in (p.get("tags") or "").split(",") if t.strip()]
        variants = p.get("variants") or []

        # 변형에서 사이즈/색상 추출
        variant_keywords = set()
        for v in variants:
            for key in ("option1", "option2", "option3"):
                val = (v.get(key) or "").strip()
                if val and val.lower() not in ("default title", "default") and len(val) <= 20:
                    variant_keywords.add(val)

        # 추천 키워드 계산
        suggested_words = []
        title_lower = title.lower()

        if product_type and product_type.lower() not in title_lower:
            suggested_words.append(product_type)
        if vendor and vendor.lower() not in title_lower and len(vendor) <= 15:
            suggested_words.append(vendor)
        for tag in tags[:3]:
            if tag.lower() not in title_lower and len(tag) <= 15:
                suggested_words.append(tag)
        for vk in list(variant_keywords)[:2]:
            if vk.lower() not in title_lower:
                suggested_words.append(vk)

        if suggested_words:
            new_title = f"{title} - {' '.join(suggested_words[:4])}"
            if len(new_title) > 150:
                new_title = new_title[:147] + "..."
            suggestions.append({
                "product_id": p["id"],
                "current_title": title,
                "suggested_title": new_title,
                "added_keywords": ", ".join(suggested_words[:4]),
            })

    filename = f"title_suggestions_{datetime.now().strftime('%Y%m%d_%H%M')}.csv"
    with open(filename, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["product_id", "current_title", "suggested_title", "added_keywords"])
        writer.writeheader()
        writer.writerows(suggestions)

    log.info(f"제안 생성 완료: {len(suggestions)}개 상품")
    log.info(f"파일: {filename}")
    log.info("👉 엑셀로 열어서 검토 후 마음에 드는 제목만 Shopify에서 직접 수정하세요.")
    log.info("   (자동 수정은 위험하므로 수동 확인을 권장합니다)")


# ─── 메인 ────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Shopify 상품 노출 최적화")
    parser.add_argument("command", choices=["audit", "fix", "suggest"], help="실행할 작업")
    args = parser.parse_args()

    required = ["SHOPIFY_STORE_URL", "SHOPIFY_ACCESS_TOKEN"]
    missing = [k for k in required if not os.getenv(k)]
    if missing:
        log.error(f".env에 누락된 환경변수: {', '.join(missing)}")
        sys.exit(1)

    store_url = os.environ["SHOPIFY_STORE_URL"]
    shopify = ShopifyClient(store_url, os.environ["SHOPIFY_ACCESS_TOKEN"])

    if args.command == "audit":
        cmd_audit(shopify, store_url)
    elif args.command == "fix":
        cmd_fix(shopify)
    elif args.command == "suggest":
        cmd_suggest(shopify)


if __name__ == "__main__":
    main()
