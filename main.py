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

# ==============================
# GitHub Secrets 자동 업데이트
# ==============================
def encrypt_secret(public_key: str, secret_value: str) -> str:
    pk = public.PublicKey(public_key.encode("utf-8"), encoding.Base64Encoder())
    sealed = public.SealedBox(pk).encrypt(secret_value.encode("utf-8"))
    return base64.b64encode(sealed).decode("utf-8")

def update_github_secret(secret_name: str, secret_value: str):
    if not GH_PAT:
        print(f"⚠️ GH_PAT 없음, {secret_name} 업데이트 스킵")
        return
    headers = {"Authorization": f"Bearer {GH_PAT}", "Accept": "application/vnd.github+json"}
    # 공개키 가져오기
    key_resp = requests.get(f"https://api.github.com/repos/{REPO}/actions/secrets/public-key", headers=headers)
    key_data = key_resp.json()
    encrypted = encrypt_secret(key_data["key"], secret_value)
    # Secret 업데이트
    resp = requests.put(
        f"https://api.github.com/repos/{REPO}/actions/secrets/{secret_name}",
        headers=headers,
        json={"encrypted_value": encrypted, "key_id": key_data["key_id"]}
    )
    if resp.status_code in [201, 204]:
        print(f"✅ GitHub Secret '{secret_name}' 자동 업데이트 완료")
    else:
        print(f"❌ Secret 업데이트 실패: {resp.status_code} {resp.text[:200]}")

# ==============================
# 카페24 토큰 갱신
# ==============================
def refresh_access_token():
    url = f"https://{MALL_ID}.cafe24api.com/api/v2/oauth/token"
    response = requests.post(url, auth=(CLIENT_ID, CLIENT_SECRET),
        data={"grant_type": "refresh_token", "refresh_token": REFRESH_TOKEN})
    data = response.json()
    if "access_token" in data:
        print(f"✅ 토큰 갱신 성공")
        # 새 토큰을 GitHub Secrets에 자동 저장
        update_github_secret("CAFE24_ACCESS_TOKEN", data["access_token"])
        update_github_secret("CAFE24_REFRESH_TOKEN", data["refresh_token"])
        return data["access_token"]
    else:
        print(f"⚠️ 토큰 갱신 실패: {data}")
        return ACCESS_TOKEN

# ==============================
# 카페24 주문 수집
# ==============================
def get_orders(token):
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    url = f"https://{MALL_ID}.cafe24api.com/api/v2/admin/orders"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    all_orders = []
    offset = 0
    while True:
        params = {"start_date": yesterday, "end_date": yesterday, "limit": 100, "offset": offset, "embed": "items"}
        response = requests.get(url, headers=headers, params=params)
        print(f"📡 상태코드: {response.status_code}")
        try:
            raw = response.json()
        except:
            print(f"❌ JSON 파싱 실패: {response.text[:300]}")
            break
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

# ==============================
# 매출 집계
# ==============================
def normalize_product_name(name: str) -> str:
    import re
    name = re.sub(r'\(.*?\)', '', name)
    name = re.sub(r'\[.*?\]', '', name)
    name = re.sub(r'[-–]\s*(Black|Brown|Clear|White|\d+ml|\d+g)', '', name, flags=re.IGNORECASE)
    return name.strip()

def extract_line_name(name: str) -> str:
    keywords = ["Hype Fit", "Hair Milk", "Scalp", "Treatment", "Serum", "Shampoo", "Conditioner", "Mascara", "Wax", "Mist"]
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
    return (dict(sorted(product_sales.items(), key=lambda x: x[1]["revenue"], reverse=True)),
            dict(sorted(line_sales.items(), key=lambda x: x[1]["revenue"], reverse=True)))

# ==============================
# Claude API 인사이트
# ==============================
def generate_insight(product_sales, line_sales, date_str):
    top_products = dict(list(product_sales.items())[:10])
    total_rev = sum(d["revenue"] for d in product_sales.values())
    total_qty = sum(d["quantity"] for d in product_sales.values())

    prompt = f"""
다음은 Narka(나르카) 카페24 쇼핑몰의 {date_str} 판매 데이터입니다.

전체 매출: {total_rev:,.0f}원 / 총 판매수량: {total_qty}개

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
    result = response.json()
    print(f"🤖 Claude API 상태: {response.status_code}")
    if "content" in result:
        return result["content"][0]["text"]
    else:
        print(f"❌ Claude API 에러: {json.dumps(result, ensure_ascii=False)[:500]}")
        # 폴백: 기본 리포트
        top5 = list(product_sales.items())[:5]
        report = f"💰 전체 매출: {total_rev:,.0f}원 | 총 {total_qty}개 판매\n\n*🏆 상품별 TOP 5*\n"
        for i, (name, data) in enumerate(top5, 1):
            report += f"{i}. {name} — {data['revenue']:,.0f}원 ({data['quantity']}개)\n"
        report += f"\n*📦 라인별 매출*\n"
        for name, data in line_sales.items():
            report += f"• {name}: {data['revenue']:,.0f}원 ({data['quantity']}개)\n"
        return report

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
    msg = requests.post("https://slack.com/api/chat.postMessage", headers=headers, json={"channel": ch, "text": message, "mrkdwn": True})
    if msg.json().get("ok"):
        print(f"✅ DM 발송 → {user_id}")
    else:
        print(f"❌ 발송 실패 ({user_id}): {msg.json()}")

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
