import os
import json
import requests
from datetime import datetime, timedelta
from collections import defaultdict

# ========================
# 환경변수 로드
# ========================
MALL_ID = os.environ["CAFE24_MALL_ID"]
CLIENT_ID = os.environ["CAFE24_CLIENT_ID"]
CLIENT_SECRET = os.environ["CAFE24_CLIENT_SECRET"]
ACCESS_TOKEN = os.environ["CAFE24_ACCESS_TOKEN"]
REFRESH_TOKEN = os.environ["CAFE24_REFRESH_TOKEN"]
SLACK_BOT_TOKEN = os.environ["SLACK_BOT_TOKEN"]
SLACK_USER_ID = os.environ["SLACK_USER_ID"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]

# ========================
# 1. 카페24 토큰 갱신
# ========================
def refresh_access_token():
    url = f"https://{MALL_ID}.cafe24api.com/api/v2/oauth/token"
    response = requests.post(
        url,
        auth=(CLIENT_ID, CLIENT_SECRET),
        data={
            "grant_type": "refresh_token",
            "refresh_token": REFRESH_TOKEN
        }
    )
    data = response.json()
    if "access_token" in data:
        print("✅ 토큰 갱신 성공")
        return data["access_token"]
    else:
        print(f"⚠️ 토큰 갱신 실패: {data}")
        return ACCESS_TOKEN  # 기존 토큰으로 시도

# ========================
# 2. 카페24 주문 데이터 수집
# ========================
def get_orders(token):
    today = datetime.now().strftime("%Y-%m-%d")
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")

    url = f"https://{MALL_ID}.cafe24api.com/api/v2/orders"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "X-Cafe24-Api-Version": "2024-03-01"
    }
    params = {
        "start_date": yesterday,
        "end_date": yesterday,
        "limit": 100,
        "embed": "items"
    }

    all_orders = []
    offset = 0

    while True:
        params["offset"] = offset
        response = requests.get(url, headers=headers, params=params)
        data = response.json()

        orders = data.get("orders", [])
        if not orders:
            break

        all_orders.extend(orders)

        if len(orders) < 100:
            break
        offset += 100

    print(f"✅ 총 {len(all_orders)}건 주문 수집 완료")
    return all_orders, yesterday

# ========================
# 3. 상품별 매출 집계
# ========================
def normalize_product_name(name: str) -> str:
    """상품명에서 옵션/색상/용량 제거해서 동일 상품끼리 묶기"""
    import re
    # 괄호 안 내용 제거
    name = re.sub(r'\(.*?\)', '', name)
    name = re.sub(r'\[.*?\]', '', name)
    # 색상/용량 옵션 제거
    name = re.sub(r'[-–]\s*(Black|Brown|Clear|White|5ml|10ml|30ml|50ml|100ml|\d+ml|\d+g)', '', name, flags=re.IGNORECASE)
    # 앞뒤 공백 제거
    name = name.strip()
    return name

def aggregate_sales(orders):
    product_sales = defaultdict(lambda: {"quantity": 0, "revenue": 0})
    line_sales = defaultdict(lambda: {"quantity": 0, "revenue": 0})

    for order in orders:
        # 취소/환불 주문 제외
        if order.get("order_status") in ["canceled", "refunded"]:
            continue

        items = order.get("items", [])
        for item in items:
            raw_name = item.get("product_name", "기타")
            qty = int(item.get("quantity", 0))
            price = float(item.get("product_price", 0)) * qty

            # 상품별 집계
            product_name = normalize_product_name(raw_name)
            product_sales[product_name]["quantity"] += qty
            product_sales[product_name]["revenue"] += price

            # 라인별 집계 (첫 번째 키워드 기준)
            line_name = extract_line_name(raw_name)
            line_sales[line_name]["quantity"] += qty
            line_sales[line_name]["revenue"] += price

    # 매출 기준 정렬
    sorted_products = dict(sorted(product_sales.items(), key=lambda x: x[1]["revenue"], reverse=True))
    sorted_lines = dict(sorted(line_sales.items(), key=lambda x: x[1]["revenue"], reverse=True))

    return sorted_products, sorted_lines

def extract_line_name(name: str) -> str:
    """라인명 추출 - Hype Fit, Hair Milk 등"""
    import re
    keywords = ["Hype Fit", "Hair Milk", "Scalp", "Treatment", "Serum", "Shampoo", "Conditioner"]
    for kw in keywords:
        if kw.lower() in name.lower():
            return kw
    return "기타"

# ========================
# 4. Claude API로 인사이트 생성
# ========================
def generate_insight(product_sales, line_sales, date_str):
    # 상위 10개 상품만 전달
    top_products = dict(list(product_sales.items())[:10])

    prompt = f"""
다음은 Narka(나르카) 카페24 쇼핑몰의 {date_str} 판매 데이터입니다.

[상품별 매출 TOP 10]
{json.dumps(top_products, ensure_ascii=False, indent=2)}

[라인별 매출 합산]
{json.dumps(line_sales, ensure_ascii=False, indent=2)}

아래 형식으로 COO 직속 담당자에게 보내는 일일 판매 브리핑을 작성해주세요:

1. 어제 전체 매출 총액 및 총 판매수량 요약 (1줄)
2. 상품별 TOP 5 순위 (매출액 + 수량)
3. 라인별 매출 현황
4. 주목할 포인트 또는 ALERT (급등/급락/이상신호 등)
5. 한줄 액션 제안

- 슬랙 메시지 형식으로 이모지 활용
- 간결하고 실무적인 톤
- 숫자는 한국식 단위(원, 개) 사용
- 전체 길이 20줄 이내
"""

    headers = {
        "x-api-key": ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json"
    }
    body = {
        "model": "claude-sonnet-4-20250514",
        "max_tokens": 1000,
        "messages": [{"role": "user", "content": prompt}]
    }

    response = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers=headers,
        json=body
    )
    result = response.json()
    return result["content"][0]["text"]

# ========================
# 5. 슬랙 DM 발송
# ========================
def send_slack_dm(user_id, message):
    headers = {
        "Authorization": f"Bearer {SLACK_BOT_TOKEN}",
        "Content-Type": "application/json"
    }

    # DM 채널 열기
    dm_response = requests.post(
        "https://slack.com/api/conversations.open",
        headers=headers,
        json={"users": user_id}
    )
    dm_data = dm_response.json()

    if not dm_data.get("ok"):
        print(f"❌ DM 채널 열기 실패: {dm_data}")
        return

    channel_id = dm_data["channel"]["id"]

    # 메시지 전송
    msg_response = requests.post(
        "https://slack.com/api/chat.postMessage",
        headers=headers,
        json={
            "channel": channel_id,
            "text": message,
            "mrkdwn": True
        }
    )
    msg_data = msg_response.json()

    if msg_data.get("ok"):
        print(f"✅ 슬랙 DM 발송 완료 → {user_id}")
    else:
        print(f"❌ 슬랙 발송 실패: {msg_data}")

# ========================
# 메인 실행
# ========================
def main():
    print("🚀 Narka 일일 리포트 시작...")

    # 1. 토큰 갱신
    token = refresh_access_token()

    # 2. 주문 데이터 수집
    orders, date_str = get_orders(token)

    if not orders:
        message = f"⚠️ *Narka 일일 리포트 | {date_str}*\n\n어제 카페24 주문 데이터가 없습니다."
        send_slack_dm(SLACK_USER_ID, message)
        return

    # 3. 매출 집계
    product_sales, line_sales = aggregate_sales(orders)

    # 4. 인사이트 생성
    insight = generate_insight(product_sales, line_sales, date_str)

    # 5. 최종 메시지 조립
    final_message = f"*📊 Narka 카페24 일일 리포트 | {date_str}*\n\n{insight}"

    # 6. 슬랙 DM 발송
    send_slack_dm(SLACK_USER_ID, final_message)

    print("✅ 모든 작업 완료!")

if __name__ == "__main__":
    main()
