import os
import json
import requests
import base64
from datetime import datetime, timedelta
from collections import defaultdict
from nacl import encoding, public

MALL_ID = os.environ["CAFE24_MALL_ID"]
CLIENT_ID = os.environ["CAFE24_CLIENT_ID"]
CLIENT_SECRET = os.environ["CAFE24_CLIENT_SECRET"]
ACCESS_TOKEN = os.environ["CAFE24_ACCESS_TOKEN"]
REFRESH_TOKEN = os.environ["CAFE24_REFRESH_TOKEN"]
SLACK_BOT_TOKEN = os.environ["SLACK_BOT_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
GH_PAT = os.environ.get("GH_PAT", "")
REPO = "narkajin/narka-daily-report"
SLACK_USER_IDS = os.environ.get("SLACK_USER_IDS", os.environ.get("SLACK_USER_ID", "")).split(",")

# Narka 라인 매핑
LINE_MAP = {
    "마스카라": "Mascara",
    "mascara": "Mascara",
    "왁스": "Wax",
    "wax": "Wax",
    "미스트": "Mist",
    "mist": "Mist",
    "밀크": "Hair Milk",
    "milk": "Hair Milk",
    "트리트먼트": "Treatment",
    "treatment": "Treatment",
    "헤어 팩": "Treatment",
    "샴푸": "Shampoo",
    "shampoo": "Shampoo",
    "세럼": "Serum",
    "serum": "Serum",
}

# ==============================
# GitHub Secrets 자동 업데이트
# ==============================
def encrypt_secret(public_key, secret_value):
    pk = public.PublicKey(public_key.encode("utf-8"), encoding.Base64Encoder())
    sealed = public.SealedBox(pk).encrypt(secret_value.encode("utf-8"))
    return base64.b64encode(sealed).decode("utf-8")

def update_github_secret(secret_name, secret_value):
    if not GH_PAT:
        return
    headers = {"Authorization": f"Bearer {GH_PAT}", "Accept": "application/vnd.github+json"}
    key_resp = requests.get(f"https://api.github.com/repos/{REPO}/actions/secrets/public-key", headers=headers)
    key_data = key_resp.json()
    encrypted = encrypt_secret(key_data["key"], secret_value)
    resp = requests.put(
        f"https://api.github.com/repos/{REPO}/actions/secrets/{secret_name}",
        headers=headers, json={"encrypted_value": encrypted, "key_id": key_data["key_id"]})
    if resp.status_code in [201, 204]:
        print(f"✅ Secret '{secret_name}' 자동 업데이트")
    else:
        print(f"❌ Secret 업데이트 실패: {resp.status_code}")

# ==============================
# 카페24 토큰 갱신
# ==============================
def refresh_access_token():
    url = f"https://{MALL_ID}.cafe24api.com/api/v2/oauth/token"
    resp = requests.post(url, auth=(CLIENT_ID, CLIENT_SECRET),
        data={"grant_type": "refresh_token", "refresh_token": REFRESH_TOKEN})
    data = resp.json()
    if "access_token" in data:
        print("✅ 토큰 갱신 성공")
        update_github_secret("CAFE24_ACCESS_TOKEN", data["access_token"])
        update_github_secret("CAFE24_REFRESH_TOKEN", data["refresh_token"])
        return data["access_token"]
    else:
        print(f"⚠️ 토큰 갱신 실패: {data}")
        return ACCESS_TOKEN

# ==============================
# 카페24 주문 수집
# ==============================
def get_orders_for_date(token, date_str):
    url = f"https://{MALL_ID}.cafe24api.com/api/v2/admin/orders"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    all_orders = []
    offset = 0
    while True:
        params = {"start_date": date_str, "end_date": date_str, "limit": 100, "offset": offset, "embed": "items"}
        resp = requests.get(url, headers=headers, params=params)
        if resp.status_code != 200:
            print(f"❌ API {resp.status_code}: {resp.text[:200]}")
            break
        raw = resp.json()
        orders = raw.get("orders", [])
        if not orders:
            break
        all_orders.extend(orders)
        if len(orders) < 100:
            break
        offset += 100
    return all_orders

def get_orders(token):
    now = datetime.now()
    yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    day_before = (now - timedelta(days=2)).strftime("%Y-%m-%d")
    same_day_last_week = (now - timedelta(days=8)).strftime("%Y-%m-%d")

    print(f"📅 어제: {yesterday} / 전일: {day_before} / 전주 동요일: {same_day_last_week}")

    orders_yesterday = get_orders_for_date(token, yesterday)
    print(f"✅ 어제 {len(orders_yesterday)}건")

    orders_day_before = get_orders_for_date(token, day_before)
    print(f"✅ 전일 {len(orders_day_before)}건")

    orders_last_week = get_orders_for_date(token, same_day_last_week)
    print(f"✅ 전주 동요일 {len(orders_last_week)}건")

    return orders_yesterday, orders_day_before, orders_last_week, yesterday

# ==============================
# 라인 분류
# ==============================
def extract_line(name):
    name_lower = name.lower()
    for keyword, line in LINE_MAP.items():
        if keyword in name_lower:
            return line
    return "기타"

# ==============================
# 매출 집계 (상품별 + 옵션별)
# ==============================
def aggregate(orders):
    product_sales = defaultdict(lambda: {"quantity": 0, "revenue": 0})
    option_sales = defaultdict(lambda: {"quantity": 0, "revenue": 0})
    line_sales = defaultdict(lambda: {"quantity": 0, "revenue": 0})
    total_revenue = 0
    total_quantity = 0

    total_canceled = 0
    total_refunded = 0

    for order in orders:
        # 취소 주문 완전 제외
        if order.get("canceled") == "T":
            cancel_amt = float(order.get("actual_order_amount", {}).get("payment_amount", 0) or 0)
            total_canceled += cancel_amt
            continue

        # 실결제 금액 - 환불금액 = 순매출
        order_payment = float(order.get("actual_order_amount", {}).get("payment_amount", 0) or 0)
        refund_amount = float(order.get("actual_order_amount", {}).get("refund_amount", 0) or 0)
        if refund_amount > 0:
            total_refunded += refund_amount
            order_payment -= refund_amount

        if order_payment <= 0:
            continue

        items = order.get("items", [])
        
        # 아이템별 실결제 금액 계산 (아이템 정가 비율로 배분)
        item_prices = []
        total_item_price = 0
        for item in items:
            qty = int(item.get("quantity", 0))
            # 실결제 기준: actual_payment > product_sale_price > product_price 순으로 시도
            actual = float(item.get("actual_payment_amount", 0) or 0)
            sale = float(item.get("product_sale_price", 0) or 0)
            orig = float(item.get("product_price", 0) or 0)
            unit_price = actual if actual > 0 else (sale if sale > 0 else orig)
            item_total = unit_price * qty
            item_prices.append(item_total)
            total_item_price += item_total
        
        for idx, item in enumerate(items):
            pname = item.get("product_name", "기타")
            option = item.get("option_value", "")
            qty = int(item.get("quantity", 0))
            
            # 주문 실결제를 아이템 비율로 배분
            if total_item_price > 0 and order_payment > 0:
                ratio = item_prices[idx] / total_item_price
                price = order_payment * ratio
            else:
                price = item_prices[idx]

            product_sales[pname]["quantity"] += qty
            product_sales[pname]["revenue"] += price

            option_key = f"{pname} [{option}]" if option else pname
            option_sales[option_key]["quantity"] += qty
            option_sales[option_key]["revenue"] += price

            line = extract_line(pname)
            line_sales[line]["quantity"] += qty
            line_sales[line]["revenue"] += price

            total_revenue += price
            total_quantity += qty

    sort_by_rev = lambda d: dict(sorted(d.items(), key=lambda x: x[1]["revenue"], reverse=True))
    return {
        "products": sort_by_rev(product_sales),
        "options": sort_by_rev(option_sales),
        "lines": sort_by_rev(line_sales),
        "total_revenue": total_revenue,
        "total_quantity": total_quantity,
        "total_canceled": total_canceled,
        "total_refunded": total_refunded
    }

# ==============================
# 비교 수치 계산
# ==============================
def calc_change(current, previous):
    if previous == 0:
        return "+NEW" if current > 0 else "0%"
    pct = ((current - previous) / previous) * 100
    sign = "+" if pct >= 0 else ""
    return f"{sign}{pct:.1f}%"

# ==============================
# 리포트 생성
# ==============================
def build_report(data_yesterday, data_day_before, data_last_week, date_str):
    y = data_yesterday
    db = data_day_before
    lw = data_last_week

    rev_change_db = calc_change(y["total_revenue"], db["total_revenue"])
    qty_change_db = calc_change(y["total_quantity"], db["total_quantity"])
    rev_change_lw = calc_change(y["total_revenue"], lw["total_revenue"])

    lines = []
    lines.append(f"💰 *순매출: {y['total_revenue']:,.0f}원* ({y['total_quantity']}개)")
    if y["total_canceled"] > 0 or y["total_refunded"] > 0:
        lines.append(f"   ↳ 취소: {y['total_canceled']:,.0f}원 / 환불: {y['total_refunded']:,.0f}원 차감 반영")
    lines.append(f"📈 전일 대비: 매출 {rev_change_db} / 수량 {qty_change_db}")
    lines.append(f"📊 전주 동요일 대비: 매출 {rev_change_lw}")
    lines.append("")

    # 상품별 TOP 5
    lines.append("*🏆 상품별 TOP 5*")
    for i, (name, d) in enumerate(list(y["products"].items())[:5], 1):
        prev = db["products"].get(name, {"revenue": 0})["revenue"]
        change = calc_change(d["revenue"], prev)
        lines.append(f"{i}. {name} — {d['revenue']:,.0f}원 ({d['quantity']}개) _{change}_")
    lines.append("")

    # 옵션별 TOP 10
    lines.append("*📦 옵션별 TOP 10*")
    for i, (name, d) in enumerate(list(y["options"].items())[:10], 1):
        lines.append(f"{i}. {name} — {d['revenue']:,.0f}원 ({d['quantity']}개)")
    lines.append("")

    # 라인별
    lines.append("*📋 라인별 매출*")
    for name, d in y["lines"].items():
        prev = db["lines"].get(name, {"revenue": 0})["revenue"]
        change = calc_change(d["revenue"], prev)
        lines.append(f"• {name}: {d['revenue']:,.0f}원 ({d['quantity']}개) _{change}_")

    return "\n".join(lines)

# ==============================
# Claude AI 인사이트
# ==============================
def generate_ai_insight(report_text, date_str):
    prompt = f"""
다음은 Narka(나르카) 카페24 쇼핑몰 {date_str} 일일 판매 리포트입니다:

{report_text}

위 데이터를 바탕으로 3줄 이내의 짧은 인사이트를 작성해주세요:
- 주목할 이상 신호 또는 기회
- 구체적인 액션 제안 1개
- 이모지 활용, 한국어, 간결한 실무 톤
"""
    headers = {"x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"}
    body = {"model": "claude-sonnet-4-20250514", "max_tokens": 500, "messages": [{"role": "user", "content": prompt}]}
    resp = requests.post("https://api.anthropic.com/v1/messages", headers=headers, json=body)
    result = resp.json()
    print(f"🤖 Claude API: {resp.status_code}")
    if "content" in result:
        return result["content"][0]["text"]
    else:
        print(f"❌ Claude 에러: {json.dumps(result, ensure_ascii=False)[:300]}")
        return None

# ==============================
# 슬랙 DM
# ==============================
def send_slack_dm(user_id, message):
    headers = {"Authorization": f"Bearer {SLACK_BOT_TOKEN}", "Content-Type": "application/json"}
    dm = requests.post("https://slack.com/api/conversations.open", headers=headers, json={"users": user_id.strip()})
    dm_data = dm.json()
    if not dm_data.get("ok"):
        print(f"❌ DM 실패 ({user_id}): {dm_data}")
        return
    ch = dm_data["channel"]["id"]
    msg = requests.post("https://slack.com/api/chat.postMessage", headers=headers,
        json={"channel": ch, "text": message, "mrkdwn": True})
    if msg.json().get("ok"):
        print(f"✅ DM → {user_id}")
    else:
        print(f"❌ 발송 실패: {msg.json()}")

def send_to_all(message):
    for uid in SLACK_USER_IDS:
        if uid.strip():
            send_slack_dm(uid.strip(), message)

# ==============================
# 메인
# ==============================
def main():
    print("🚀 Narka 일일 리포트 시작...")
    token = refresh_access_token()

    orders_y, orders_db, orders_lw, date_str = get_orders(token)

    if not orders_y:
        send_to_all(f"⚠️ *Narka 일일 리포트 | {date_str}*\n\n어제 카페24 주문 데이터가 없습니다.")
        return

    data_y = aggregate(orders_y)
    data_db = aggregate(orders_db)
    data_lw = aggregate(orders_lw)

    report = build_report(data_y, data_db, data_lw, date_str)

    # AI 인사이트 추가
    ai_insight = generate_ai_insight(report, date_str)
    if ai_insight:
        report += f"\n\n*💡 AI 인사이트*\n{ai_insight}"

    final = f"*📊 Narka 카페24 일일 리포트 | {date_str}*\n\n{report}"
    send_to_all(final)
    print("✅ 완료!")

if __name__ == "__main__":
    main()
