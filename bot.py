"""
캠코양재타워 구내식당 점심 메뉴 텔레그램 봇
- 네이버 플레이스 '소식'에 올라오는 주간 식단표를 자동으로 가져와서 Claude가 날짜별로 읽어 저장
- 평일 10:30에 그날 메뉴 전송
- 네이버에서 못 가져오는 날엔 봇한테 식단표 사진을 직접 보내도 똑같이 처리
- 봇을 그룹방에 초대하면 자동으로 그 방에도 전송 (내보내면 자동 중단, /stop으로도 끌 수 있음)
- "@봇 오늘의 메뉴" / "@봇 내일 메뉴" / /menu 로 물어보면 답장
GitHub Actions로 실행: 평일 08:00~13:30은 대기 모드(바로 답장), 그 외엔 매시 정각에 한 번씩
"""
import base64
import datetime as dt
import html
import io
import json
import os
import re
import sys
import time
from urllib.parse import parse_qs, unquote, urlparse

import requests
from PIL import Image, ImageOps

def env(name, default=""):
    return (os.environ.get(name) or default).strip()  # Secrets에 딸려 들어간 줄바꿈 제거


TG_TOKEN = env("TELEGRAM_TOKEN")
ANTHROPIC_KEY = env("ANTHROPIC_API_KEY")
ADMIN_ID = int(env("ADMIN_ID"))                        # 관리자 (나)
CHAT_ID = env("CHAT_ID")                               # 선택: 고정으로 받을 방 하나 더
PLACE_ID = env("NAVER_PLACE_ID", "1125744770")
MODEL = env("CLAUDE_MODEL", "claude-sonnet-5-5")
SEND_AFTER = env("SEND_AFTER", "10:30")                # 메뉴 보내는 시각 (KST)
STATE_FILE = "state.json"

LISTEN_UNTIL = env("LISTEN_UNTIL", "13:30")            # 대기 모드 끝나는 시각 (KST)

KST = dt.timezone(dt.timedelta(hours=9))
NOW = dt.datetime.now(KST)
TODAY = NOW.strftime("%Y-%m-%d")
BOT_USERNAME = ""


def refresh_clock():
    global NOW, TODAY
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

def tg(method, _timeout=30, **params):
    r = requests.post(f"{TG_API}/{method}", data=params, timeout=_timeout)
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


def menu_text(menu, day=None):
    day = day or NOW
    label = f"{day.month}/{day.day} ({WEEKDAY[day.weekday()]})"
    return f"🍚 캠코양재타워 구내식당 {label}\n\n{bullets(menu)}"


def send_to(state, chat_id, text, reply_to=None):
    """보내다가 봇이 쫓겨났거나 방이 없어졌으면 목록에서 뺌"""
    try:
        extra = {"reply_to_message_id": reply_to, "allow_sending_without_reply": "true"} if reply_to else {}
        tg("sendMessage", chat_id=chat_id, text=text, **extra)
    except Exception as e:
        print(f"{chat_id} 전송 실패:", e)
        if any(k in str(e) for k in ("kicked", "blocked", "chat not found", "not a member", "deactivated")):
            state["chats"].pop(str(chat_id), None)


def subscribe(state, chat):
    cid = str(chat["id"])
    if cid in state["chats"]:
        return
    name = chat.get("title") or chat.get("first_name") or "개인"
    state["chats"][cid] = name
    if cid != str(ADMIN_ID):
        notify_admin(f"'{name}' 방에도 점심 메뉴를 보내기 시작해요.")
    welcome = "🍚 점심 메뉴 봇이에요. 평일 10:30에 캠코양재타워 구내식당 메뉴를 보내드려요.\n그만 받으려면 /stop"
    today_menu = state["menu"].get(TODAY)
    if today_menu and NOW.weekday() < 5 and NOW.hour < 14:  # 점심시간 전이면 오늘 메뉴도 바로
        welcome += "\n\n" + menu_text(today_menu)
    send_to(state, cid, welcome)


def unsubscribe(state, chat_id, reply=False):
    name = state["chats"].pop(str(chat_id), None)
    if name and str(chat_id) != str(ADMIN_ID):
        notify_admin(f"'{name}' 방 전송을 멈췄어요.")
    if reply:
        send_to(state, chat_id, "메뉴 알림을 껐어요. 다시 받으려면 /start")


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            state = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        state = {}
    for key, default in (("offset", 0), ("menu", {}), ("sent", {}), ("naver_read", []), ("naver_alert", "")):
        state.setdefault(key, default)
    if "chats" not in state:  # 메뉴 받는 방 목록 {방번호: 이름}
        state["chats"] = {str(ADMIN_ID): "나"}
        if CHAT_ID:
            state["chats"][CHAT_ID] = "CHAT_ID"
    return state


def save_state(state):
    cutoff = (NOW - dt.timedelta(days=45)).strftime("%Y-%m-%d")
    for key in ("menu", "sent"):
        state[key] = {d: v for d, v in state[key].items() if d >= cutoff}
    state.pop("naver_seen", None)  # 이전 버전 기록은 버림
    state["naver_read"] = state["naver_read"][-500:]
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
        # 사장님이 올린 사진(ldb-phinf)만. 방문자 리뷰, 블로그, 클립 썸네일은 제외
        if urlparse(url).netloc == "ldb-phinf.pstatic.net" and url not in found:
            found.append(url)
    # 주소에 들어 있는 업로드 날짜(예: /20261016_94/) 기준 최신순
    def uploaded(url):
        m = re.search(r"/(\d{8})_", url)
        return m.group(1) if m else "0"
    return sorted(found, key=uploaded, reverse=True)


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

    first_run = not state["naver_read"]
    new = [u for u in images if u not in state["naver_read"]]
    limit = 3 if first_run else 6  # 첫 실행은 최신 3장만 (이번 주 식단표 찾기용)
    print(f"네이버 사장님 사진 {len(images)}개 중 새 사진 {len(new)}개, {min(limit, len(new))}개 읽음")
    for url in new[:limit]:
        try:
            days = read_menu(to_b64(requests.get(url, headers=WEB_HEADERS, timeout=60).content))
        except Exception as e:
            print("사진 읽기 실패:", url, e)
            continue  # 실패한 사진은 다음 실행 때 다시 시도
        state["naver_read"].append(url)
        print("읽음:", url, f"→ 식단 {len(days)}일치")
        if days:
            save_days(state, days, "네이버 소식")
    if first_run:  # 첫 실행 때 나머지 옛날 사진은 건너뜀
        state["naver_read"].extend(new[limit:])


# ---------- 텔레그램 메시지 처리 (메뉴 질문, 방 추가/삭제, 직접 보낸 식단표) ----------

def is_menu_question(msg, text, cmd):
    if cmd == "/menu":
        return True
    if "메뉴" not in text:
        return False
    if msg["chat"]["type"] == "private":
        return True
    me = BOT_USERNAME.lower()
    mentioned = bool(me) and f"@{me}" in text.lower()
    replied = msg.get("reply_to_message", {}).get("from", {}).get("username", "").lower() == me
    return mentioned or replied


def answer_menu(state, msg, text):
    day = NOW + dt.timedelta(days=1) if "내일" in text else NOW
    key = day.strftime("%Y-%m-%d")
    menu = state["menu"].get(key)
    when = "내일" if "내일" in text else "오늘"
    if menu:
        reply = menu_text(menu, day)
    elif day.weekday() >= 5:
        reply = f"{when}은 주말이라 구내식당이 쉬어요."
    else:
        reply = f"{when}({day.month}/{day.day}) 메뉴는 아직 없어요. 휴무이거나 식단표가 안 올라왔어요."
    send_to(state, msg["chat"]["id"], reply, reply_to=msg["message_id"])


def handle_updates(state, wait=0):
    updates = tg("getUpdates", _timeout=wait + 15, offset=state["offset"], timeout=wait,
                 allowed_updates='["message","my_chat_member"]')
    for u in updates:
        state["offset"] = u["update_id"] + 1

        if "my_chat_member" in u:  # 봇이 방에 초대되거나 쫓겨남
            m = u["my_chat_member"]
            status = m["new_chat_member"]["status"]
            if status in ("member", "administrator"):
                subscribe(state, m["chat"])
            elif status in ("left", "kicked"):
                unsubscribe(state, m["chat"]["id"])
            continue

        msg = u.get("message")
        if not msg:
            continue
        chat = msg["chat"]
        if msg.get("migrate_to_chat_id"):  # 일반 그룹이 슈퍼그룹으로 바뀌면 방 번호가 바뀜
            name = state["chats"].pop(str(chat["id"]), None)
            if name:
                state["chats"][str(msg["migrate_to_chat_id"])] = name
            continue

        text = (msg.get("text") or "").strip()
        cmd = text.split()[0].split("@")[0].lower() if text.startswith("/") else ""
        if cmd == "/stop":
            unsubscribe(state, chat["id"], reply=True)
            continue
        if chat["type"] != "private" or cmd == "/start":
            subscribe(state, chat)  # 그룹방 메시지나 /start → 방 등록
        if is_menu_question(msg, text, cmd):
            answer_menu(state, msg, text)
            continue
        if chat["type"] != "private" or msg.get("from", {}).get("id") != ADMIN_ID:
            continue

        # 여기부터는 관리자(나) 개인 대화
        if msg.get("photo"):
            file_id = msg["photo"][-1]["file_id"]
        elif msg.get("document", {}).get("mime_type", "").startswith("image/"):
            file_id = msg["document"]["file_id"]
        else:
            rooms = ", ".join(state["chats"].values())
            notify_admin(f"지금 메뉴 받는 방: {rooms}\n\n'메뉴'라고 보내면 오늘 메뉴를 알려드려요.\n"
                         "식단표를 직접 넣으려면 사진을 '파일'로 보내주세요.")
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
        for chat_id in list(state["chats"]):
            send_to(state, chat_id, menu_text(menu))
    else:
        notify_admin(f"오늘({TODAY}) 메뉴가 없어요. 휴무가 아니면 식단표 사진을 봇한테 보내주세요.")
    state["sent"][TODAY] = True


def load_bot_name():
    global BOT_USERNAME
    try:
        BOT_USERNAME = tg("getMe")["username"]
    except Exception as e:
        print("봇 이름 확인 실패:", e)


def run_once():
    """매시 정각 실행: 한 번 훑고 끝"""
    state = load_state()
    try:
        handle_updates(state)
        collect_naver(state)
        send_today(state)
    finally:
        save_state(state)


def listen():
    """평일 오전 대기 모드: 질문에 바로 답하고, 10:30 전송과 정각 네이버 확인도 여기서"""
    state = load_state()
    naver_hour = None
    try:
        while True:
            refresh_clock()
            if NOW.strftime("%H:%M") >= LISTEN_UNTIL:
                break
            try:
                if naver_hour != NOW.hour:
                    collect_naver(state)
                    naver_hour = NOW.hour
                send_today(state)
                handle_updates(state, wait=50)  # 메시지가 오면 바로 깨어남
                save_state(state)
            except Exception as e:
                print("대기 중 오류, 잠시 후 재시도:", e)
                time.sleep(10)
    finally:
        save_state(state)


def main():
    load_bot_name()
    if len(sys.argv) > 1 and sys.argv[1] == "listen":
        listen()
    else:
        run_once()


if __name__ == "__main__":
    main()
