import os
import json
import requests
from datetime import datetime, timedelta
from collections import defaultdict

MALL_ID = os.environ["CAFE24_MALL_ID"]
CLIENT_ID = os.environ["CAFE24_CLIENT_ID"]
CLIENT_SECRET = os.environ["CAFE24_CLIENT_SECRET"]
ACCESS_TOKEN = os.environ["CAFE24_ACCESS_TOKEN"]
REFRESH_TOKEN = os.environ["CAFE24_REFRESH_TOKEN"]
SLACK_BOT_TOKEN = os.environ["SLACK_BOT_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
SLACK_USER_IDS = os.environ.get("SLACK_USER_IDS", os.environ.get("SLACK_USER_ID", "")).split(",")

def refresh_access_token():
    url = f"https://{MALL_ID}.cafe24api.com/api/v2/oauth/token"
    response = requests.post(url, auth=(CLIENT_ID, CLIENT_SECRET),
        data={"grant_type": "refresh_token", "refresh_token": REFRESH_TOKEN})
    data = response.json()
    if "access_token" in data:
        print(f"✅ 토큰 갱신 성공")
        return data["access_token"]
    else:
        print(f"⚠️ 토큰 갱신 실패, 기존 토큰 사용: {data}")
        return ACCESS_TOKEN

def get_orders(token):
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    # 핵심 수정: /api/v2/admin/orders (admin 추가!)
    url = f"https://{MALL_ID}.cafe24api.com/api/v2/admin/orders"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }
    all_orders = []
    offset = 0

    while True:
        params = {
            "start_date": yesterday,
            "end_date": yesterday,
            "limit": 100,
            "offset": offset,
            "embed": "items"
        }
        response = requests.get(url, headers=headers, params=params)
        print(f"📡 상태코드: {response.status_code}")

        try:
            raw = response.json()
        except:
            print(f"❌ JSON 파싱 실패: {response.text[:300]}")
            break

        print(f"📦 응답 키: {list(raw.keys()) if isinstance(raw, dict) else 'not dict'}")

        orders = raw.get("orders", [])
        if not orders:
            if "error" in raw:
                print(f"❌ API 에러: {raw['error']}")
            break

        all_orders.extend(orders)
        if len(orders) < 100:
            break
        offset += 100

    print(f"✅ 총 {len(all_orders)}건 수집")
    return all_orders, yesterday

def normalize_product_name(name: str) -> str:
    import re
    name = re.sub(r'\(.*?\)', '', name)
    name = re.sub(r'\[.*?\]', '', name)
    name = re.sub(r'[-–]\s*(Black|Brown|Clear|White|\d+ml|\d+g)', '', name, flags=re.IGNORECASE)
    return name.strip()

def extract_line_name(name: str) -> str:
    keywords = ["Hype Fit", "Hair Milk", "Scalp", "Treatment", "Serum", "Shampoo", "Conditioner"]
    for kw in keywords:
        if kw.lower() in name.lower():
            return kw
    return "기타"

def aggregate_sales(orders):
    product_sales = defaultdict(lambda: {"quantity": 0, "revenue": 0})
    line_sales = defaultdict(lambda: {"quantity": 0, "revenue": 0})

    for order in orders:
        items = order.get("items", [])
        for item in items:
            raw_name = item.get("product_name", "기타")
            qty = int(item.get("quantity", 0))
            price = float(item.get("product_price", 0)) * qty

            pname = normalize_product_name(raw_name)
            product_sales[pname]["quantity"] += qty
            product_sales[pname]["revenue"] += price

            lname = extract_line_name(raw_name)
            line_sales[lname]["quantity"] += qty
            line_sales[lname]["revenue"] += price

    sorted_products = dict(sorted(product_sales.items(), key=lambda x: x[1]["revenue"], reverse=True))
    sorted_lines = dict(sorted(line_sales.items(), key=lambda x: x[1]["revenue"], reverse=True))
    return sorted_products, sorted_lines

def generate_insight(product_sales, line_sales, date_str):
    top_products = dict(list(product_sales.items())[:10])
    prompt = f"""
다음은 Narka(나르카) 카페24 쇼핑몰의 {date_str} 판매 데이터입니다.

[상품별 매출 TOP 10]
{json.dumps(top_products, ensure_ascii=False, indent=2)}

[라인별 매출 합산]
{json.dumps(line_sales, ensure_ascii=False, indent=2)}

COO 직속 담당자에게 보내는 일일 판매 브리핑을 작성해주세요:
1. 전체 매출 총액 및 총 판매수량 요약 (1줄)
2. 상품별 TOP 5 순위 (매출액 + 수량)
3. 라인별 매출 현황
4. 주목할 포인트 또는 ALERT
5. 한줄 액션 제안

- 슬랙 메시지 형식, 이모지 활용
- 간결하고 실무적인 톤
- 숫자는 한국식 단위(원, 개)
- 20줄 이내
"""
    headers = {"x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"}
    body = {"model": "claude-sonnet-4-20250514", "max_tokens": 1000, "messages": [{"role": "user", "content": prompt}]}
    response = requests.post("https://api.anthropic.com/v1/messages", headers=headers, json=body)
    return response.json()["content"][0]["text"]

def send_slack_dm(user_id, message):
    headers = {"Authorization": f"Bearer {SLACK_BOT_TOKEN}", "Content-Type": "application/json"}
    dm = requests.post("https://slack.com/api/conversations.open", headers=headers, json={"users": user_id.strip()})
    dm_data = dm.json()
    if not dm_data.get("ok"):
        print(f"❌ DM 실패 ({user_id}): {dm_data}")
        return
    ch = dm_data["channel"]["id"]
    msg = requests.post("https://slack.com/api/chat.postMessage", headers=headers, json={"channel": ch, "text": message, "mrkdwn": True})
    if msg.json().get("ok"):
        print(f"✅ DM 발송 → {user_id}")
    else:
        print(f"❌ 발송 실패 ({user_id}): {msg.json()}")

def send_to_all(message):
    for uid in SLACK_USER_IDS:
        if uid.strip():
            send_slack_dm(uid.strip(), message)

def main():
    print("🚀 Narka 일일 리포트 시작...")
    token = refresh_access_token()
    orders, date_str = get_orders(token)

    if not orders:
        send_to_all(f"⚠️ *Narka 일일 리포트 | {date_str}*\n\n어제 카페24 주문 데이터가 없습니다.")
        return

    product_sales, line_sales = aggregate_sales(orders)
    insight = generate_insight(product_sales, line_sales, date_str)
    send_to_all(f"*📊 Narka 카페24 일일 리포트 | {date_str}*\n\n{insight}")
    print("✅ 완료!")

if __name__ == "__main__":
    main()
