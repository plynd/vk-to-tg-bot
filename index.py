"""
VK -> Telegram: пересылка сообщений из бесед VK для Yandex Cloud Functions.

Функция запускается таймером раз в минуту, берёт из VK новые сообщения выбранных
бесед и отправляет их в Telegram. Id последнего обработанного сообщения хранится
в Object Storage (файл state.json), потому что между запусками функция ничего не помнит.
Зависимостей нет: только стандартная библиотека Python.

Переменные окружения:
  VK_TOKEN      токен пользователя VK (scope: messages, offline)
  TG_BOT_TOKEN  токен бота от @BotFather
  TG_CHAT_ID    твой chat_id в Telegram
  VK_PEER_IDS   peer_id бесед через запятую (например 2000000012). Если пусто, функция
                возвращает в ответе (и пишет в лог) список твоих бесед с их peer_id.
  BUCKET        имя бакета Object Storage для state.json
  STATE_KEY     необязательно: имя файла состояния (по умолчанию state.json)

Точка входа функции: index.handler
"""
import json
import os
import urllib.error
import urllib.parse
import urllib.request

VK_API = "https://api.vk.com/method/"
VK_VERSION = "5.199"
STORAGE = "https://storage.yandexcloud.net"


def env(name, default=""):
    return os.environ.get(name, default)


def http(url, data=None, headers=None, method=None, timeout=15):
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


# ---------- VK ----------

def vk(method, **params):
    params.update(access_token=env("VK_TOKEN"), v=VK_VERSION)
    raw = http(VK_API + method, data=urllib.parse.urlencode(params).encode())
    data = json.loads(raw)
    if "error" in data:
        raise RuntimeError(f"VK {method}: {data['error'].get('error_msg')}")
    return data["response"]


def chat_title(peer_id):
    try:
        info = vk("messages.getConversationsById", peer_ids=peer_id)["items"][0]
        return info["chat_settings"]["title"]
    except Exception:
        return str(peer_id)


def best_photo(photo):
    return max(photo["sizes"], key=lambda s: s["width"] * s["height"])["url"]


PAGE_SIZE = 20   # сколько сообщений читаем за один запрос
MAX_PAGES = 10   # потолок: не больше PAGE_SIZE * MAX_PAGES = 200 сообщений за запуск


def fetch_new(peer, last):
    """Читает историю беседы страницами по PAGE_SIZE и останавливается, когда дошла
    до уже обработанного сообщения (last). В обычную минуту это один запрос;
    при всплеске дочитывает следующие страницы. Возвращает (сообщения, имена)."""
    items, names = {}, {}
    offset = 0
    for _ in range(MAX_PAGES):
        hist = vk("messages.getHistory", peer_id=peer, count=PAGE_SIZE, offset=offset, extended=1)
        page = hist["items"]
        for p in hist.get("profiles", []):
            names[p["id"]] = f"{p['first_name']} {p['last_name']}"
        for m in page:
            items[m["id"]] = m  # словарь по id убирает дубли, если сообщения сдвинули страницы
        # первый запуск (last is None) или дошли до обработанного: хватит
        if not page or last is None or min(m["id"] for m in page) <= last:
            break
        offset += PAGE_SIZE
    return list(items.values()), names


def discover():
    """Режим поиска: печатает в лог и возвращает список бесед с их peer_id."""
    res = vk("messages.getConversations", count=50)
    lines = []
    for item in res["items"]:
        conv = item["conversation"]
        if conv["peer"]["type"] == "chat":
            line = f"peer_id={conv['peer']['id']}  беседа: {conv['chat_settings']['title']}"
            print(line)
            lines.append(line)
    return "\n".join(lines) or "беседы не найдены"


# ---------- Telegram ----------

def tg(method, **params):
    url = f"https://api.telegram.org/bot{env('TG_BOT_TOKEN')}/{method}"
    params.setdefault("chat_id", env("TG_CHAT_ID"))
    try:
        raw = http(url, data=urllib.parse.urlencode(params).encode(), timeout=20)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Telegram {method}: {e.read().decode(errors='ignore')}")
    return json.loads(raw)


def build_text(msg, names, title):
    def name(uid):
        return names.get(uid, str(uid))

    body = msg.get("text", "")
    lines = []
    if msg.get("reply_message"):
        r = msg["reply_message"]
        lines.append(f"↩️ {name(r['from_id'])}: {r.get('text', '')[:200]}")
    for fwd in msg.get("fwd_messages", []):
        lines.append(f"➡️ {name(fwd['from_id'])}: {fwd.get('text', '')}")

    photos, other = [], []
    for att in msg.get("attachments", []):
        kind = att["type"]
        if kind == "photo":
            photos.append(best_photo(att["photo"]))
        elif kind == "doc":
            other.append(f"📎 {att['doc']['title']}: {att['doc']['url']}")
        else:
            other.append(f"[{kind}]")

    head = f"💬 {title}\n👤 {name(msg['from_id'])}"
    text = "\n".join([head, "", body] + lines + other).strip()
    return text[:4000], photos


# ---------- состояние в Object Storage ----------

def state_url():
    return f"{STORAGE}/{env('BUCKET')}/{env('STATE_KEY', 'state.json')}"


def load_state(iam):
    try:
        raw = http(state_url(), headers={"Authorization": f"Bearer {iam}"})
        return json.loads(raw)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return {}
        raise


def save_state(iam, state):
    http(
        state_url(),
        data=json.dumps(state).encode(),
        headers={"Authorization": f"Bearer {iam}", "Content-Type": "application/json"},
        method="PUT",
    )


# ---------- основная логика ----------

def handler(event, context):
    peers = [int(x) for x in env("VK_PEER_IDS").split(",") if x.strip()]
    if not peers:
        return {"statusCode": 200, "body": discover()}

    iam = context.token["access_token"]
    state = load_state(iam)
    changed = False

    for peer in peers:
        key = str(peer)
        last = state.get(key)
        items, names = fetch_new(peer, last)
        if not items:
            continue
        newest = max(m["id"] for m in items)

        if last is None:
            # первый запуск: запоминаем текущую позицию, старое не пересылаем
            state[key] = newest
            changed = True
            continue

        cursor = last
        fresh = sorted((m for m in items if m["id"] > last), key=lambda m: m["id"])
        title = chat_title(peer) if fresh else ""
        for msg in fresh:
            if not msg.get("out"):  # свои сообщения не пересылаем
                try:
                    text, photos = build_text(msg, names, title)
                    tg("sendMessage", text=text)
                    for url in photos:
                        tg("sendPhoto", photo=url)
                except Exception as e:
                    print("Ошибка отправки, продолжу в следующий запуск:", e)
                    break
            cursor = msg["id"]
        if cursor != last:
            state[key] = cursor
            changed = True

    if changed:
        save_state(iam, state)
    return {"statusCode": 200, "body": "ok"}