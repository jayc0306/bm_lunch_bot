"""
캠코양재타워 구내식당 점심 메뉴 텔레그램 봇
- 네이버 플레이스 '소식'에 올라오는 주간 식단표를 자동으로 가져와서 Claude가 날짜별로 읽어 저장
- 평일 10:30에 그날 메뉴 전송
- 네이버에서 못 가져오는 날엔 봇한테 식단표 사진을 직접 보내도 똑같이 처리
GitHub Actions가 주기적으로 실행 (상시 서버 없음)
"""
import base64
import datetime as dt
import html
import io
import json
import os
import re
from urllib.parse import parse_qs, unquote, urlparse

import requests
from PIL import Image, ImageOps

TG_TOKEN = os.environ["TELEGRAM_TOKEN"]
ANTHROPIC_KEY = os.environ["ANTHROPIC_API_KEY"]
ADMIN_ID = int(os.environ["ADMIN_ID"])                 # 관리자 (나)
CHAT_ID = os.environ.get("CHAT_ID") or str(ADMIN_ID)   # 메뉴 받을 곳 (나 / 그룹 / @채널)
PLACE_ID = os.environ.get("NAVER_PLACE_ID") or "1125744770"
MODEL = os.environ.get("CLAUDE_MODEL") or "claude-sonnet-5-5"
SEND_AFTER = os.environ.get("SEND_AFTER") or "10:30"   # 메뉴 보내는 시각 (KST)
STATE_FILE = "state.json"

KST = dt.timezone(dt.timedelta(hours=9))
NOW = dt.datetime.now(KST)
TODAY = NOW.strftime("%Y-%m-%d")
WEEKDAY = "월화수목금토일"
TG_API = f"https://api.telegram.org/bot{TG_TOKEN}"
WEB_HEADERS = {
    "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 "
                  "(KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1",
    "Accept-Language": "ko-KR,ko;q=0.9",
    "Referer": "https://m.place.naver.com/",
}

PROMPT = """이 사진이 캠코양재타워 구내식당의 식단표(주간 또는 월간)인지 보고, 맞으면 날짜별 중식 메뉴를 읽어서 JSON 객체 하나만 출력해. 설명이나 코드블록은 쓰지 마.

{{"days": {{"YYYY-MM-DD": ["메뉴1", "메뉴2"]}}}}

- 표에 적힌 월/일을 쓰고, 연도가 안 보이면 오늘({received})에 가장 가까운 연도로 정해
- 조식/석식이 같이 있으면 중식만. 코너가 여러 개면 "한식: 제육볶음"처럼 코너명을 앞에 붙여
- 쌀밥, 배추김치처럼 매일 똑같이 나오는 기본 반찬은 빼
- 메뉴는 표에 적힌 순서대로
- 휴무이거나 비어 있는 날은 빼
- 글씨가 흐려서 확실하지 않은 메뉴는 이름 뒤에 (?)를 붙여
- 식단표가 아닌 사진(음식 사진, 매장 사진, 공지 등)이면 {{"days": {{}}}}"""


# ---------- 공통 ----------

def tg(method, **params):
    r = requests.post(f"{TG_API}/{method}", data=params, timeout=30)
    if not r.ok:
        raise RuntimeError(f"Telegram {method} 실패: {r.text}")
    return r.json()["result"]


def notify_admin(text):
    try:
        tg("sendMessage", chat_id=ADMIN_ID, text=text)
    except Exception as e:
        print("관리자 알림 실패:", e)


def bullets(items):
    return "\n".join(f"· {m}" for m in items)


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            state = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        state = {}
    for key, default in (("offset", 0), ("menu", {}), ("sent", {}), ("naver_seen", []), ("naver_alert", "")):
        state.setdefault(key, default)
    return state


def save_state(state):
    cutoff = (NOW - dt.timedelta(days=45)).strftime("%Y-%m-%d")
    for key in ("menu", "sent"):
        state[key] = {d: v for d, v in state[key].items() if d >= cutoff}
    state["naver_seen"] = state["naver_seen"][-500:]
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2, sort_keys=True)


def to_b64(raw):
    img = ImageOps.exif_transpose(Image.open(io.BytesIO(raw))).convert("RGB")
    img.thumbnail((2400, 2400))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=90)
    return base64.b64encode(buf.getvalue()).decode()


def read_menu(image_b64):
    r = requests.post(
        "https://api.anthropic.com/v1/messages",
        json={
            "model": MODEL,
            "max_tokens": 6000,
            "messages": [{"role": "user", "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": image_b64}},
                {"type": "text", "text": PROMPT.format(received=TODAY)},
            ]}],
        },
        timeout=180,
        headers={"x-api-key": ANTHROPIC_KEY, "anthropic-version": "2023-06-01"},
    )
    if not r.ok:
        raise RuntimeError(f"Claude API 실패: {r.text[:300]}")
    text = "".join(b.get("text", "") for b in r.json()["content"])
    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        raise ValueError(f"응답에서 JSON을 못 찾음: {text[:200]}")
    return json.loads(match.group(0)).get("days", {})


def save_days(state, days, where):
    state["menu"].update(days)
    first, last = min(days), max(days)
    check = min((d for d in days if d >= TODAY), default=first)
    notify_admin(f"[{where}] 식단 {len(days)}일치 저장했어요 ({first} ~ {last})\n\n확인용 {check}:\n{bullets(days[check])}")


# ---------- 네이버 플레이스 소식 자동 수집 ----------

def normalize(url):
    """썸네일 주소를 원본 이미지 주소로"""
    url = html.unescape(url)
    query = parse_qs(urlparse(url).query)
    if "src" in query:  # search.pstatic.net/common/?src=원본주소 형태
        return normalize(unquote(query["src"][0]))
    return url.split("?")[0]


def naver_images():
    page = requests.get(f"https://m.place.naver.com/restaurant/{PLACE_ID}/feed",
                        headers=WEB_HEADERS, timeout=30)
    if page.status_code != 200:
        raise RuntimeError(f"네이버 응답 코드 {page.status_code}")
    text = page.text.replace("\\u002F", "/").replace("\\/", "/")
    found = []
    for raw in re.findall(r'https?://[a-z0-9.-]+\.pstatic\.net/[^"\'\s<>\\)]+', text, re.I):
        url = normalize(raw)
        if "phinf" in urlparse(url).netloc and url not in found:  # 업로드된 사진만 (아이콘 등 제외)
            found.append(url)
    return found


def collect_naver(state):
    try:
        images = naver_images()
        if not images:
            raise RuntimeError("페이지에서 사진을 못 찾음")
    except Exception as e:
        print("네이버 수집 실패:", e)
        last = state["naver_alert"]
        if not last or (NOW.date() - dt.date.fromisoformat(last)).days >= 7:
            notify_admin(f"네이버 소식에서 식단표를 못 가져왔어요 ({e}).\n당분간은 식단표 사진을 봇한테 직접 보내주세요.")
            state["naver_alert"] = TODAY
        return

    first_run = not state["naver_seen"]
    new = [u for u in images if u not in state["naver_seen"]]
    print(f"네이버 사진 {len(images)}개 중 새 사진 {len(new)}개")
    for url in new[:12 if first_run else 6]:
        try:
            days = read_menu(to_b64(requests.get(url, headers=WEB_HEADERS, timeout=60).content))
        except Exception as e:
            print("사진 읽기 실패:", url, e)
            continue
        state["naver_seen"].append(url)
        if days:
            save_days(state, days, "네이버 소식")
    if first_run:  # 첫 실행 때 나머지 옛날 사진은 건너뜀
        state["naver_seen"].extend(u for u in new if u not in state["naver_seen"])


# ---------- 직접 보낸 사진 (백업 경로) ----------

def ingest_manual(state):
    updates = tg("getUpdates", offset=state["offset"], timeout=0, allowed_updates='["message"]')
    for u in updates:
        state["offset"] = u["update_id"] + 1
        msg = u.get("message")
        if not msg or msg["chat"]["type"] != "private" or msg.get("from", {}).get("id") != ADMIN_ID:
            continue

        if msg.get("photo"):
            file_id = msg["photo"][-1]["file_id"]
        elif msg.get("document", {}).get("mime_type", "").startswith("image/"):
            file_id = msg["document"]["file_id"]
        else:
            notify_admin("식단표 사진을 보내주세요.\n'파일'로 보내면 화질이 안 깨져서 더 정확해요.")
            continue

        try:
            path = tg("getFile", file_id=file_id)["file_path"]
            raw = requests.get(f"https://api.telegram.org/file/bot{TG_TOKEN}/{path}", timeout=60).content
            days = read_menu(to_b64(raw))
        except Exception as e:
            notify_admin(f"사진을 못 읽었어요. 다시 보내주세요.\n({e})")
            continue
        if days:
            save_days(state, days, "직접 보냄")
        else:
            notify_admin("식단표를 못 찾았어요. 표 전체가 잘 보이게 다시 보내주세요.")


# ---------- 전송 ----------

def send_today(state):
    """평일 10:30 이후 첫 실행 때 오늘 메뉴 전송"""
    if NOW.weekday() >= 5 or NOW.strftime("%H:%M") < SEND_AFTER or state["sent"].get(TODAY):
        return
    menu = state["menu"].get(TODAY)
    if menu:
        label = f"{NOW.month}/{NOW.day} ({WEEKDAY[NOW.weekday()]})"
        tg("sendMessage", chat_id=CHAT_ID, text=f"🍚 캠코양재타워 구내식당 {label}\n\n{bullets(menu)}")
    else:
        notify_admin(f"오늘({TODAY}) 메뉴가 없어요. 휴무가 아니면 식단표 사진을 봇한테 보내주세요.")
    state["sent"][TODAY] = True


def main():
    state = load_state()
    try:
        ingest_manual(state)
        collect_naver(state)
        send_today(state)
    finally:
        save_state(state)


if __name__ == "__main__":
    main()
