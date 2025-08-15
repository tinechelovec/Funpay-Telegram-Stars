import os
import logging
import time
import json
import re
import requests
from dotenv import load_dotenv
from FunPayAPI import Account
from FunPayAPI.updater.runner import Runner
from FunPayAPI.updater.events import NewOrderEvent, NewMessageEvent

load_dotenv()

# Логгирование
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

COOLDOWN_SECONDS = 1
TOKEN_FILE = "auth_token.json"
FRAGMENT_API_URL = "https://api.fragment-api.com/v1"
waiting_for_nick = {}

# Fragment auth
FRAGMENT_TOKEN = None
FRAGMENT_API_KEY = os.getenv("FRAGMENT_API_KEY")
FRAGMENT_PHONE = os.getenv("FRAGMENT_PHONE")
FRAGMENT_MNEMONICS = os.getenv("FRAGMENT_MNEMONICS")


def load_fragment_token():
    if os.path.exists(TOKEN_FILE):
        with open(TOKEN_FILE, "r") as f:
            return json.load(f).get("token")
    return None


def save_fragment_token(token):
    with open(TOKEN_FILE, "w") as f:
        json.dump({"token": token}, f)


def authenticate_fragment():
    try:
        mnemonics_list = FRAGMENT_MNEMONICS.strip().split()
        payload = {
            "api_key": FRAGMENT_API_KEY,
            "phone_number": FRAGMENT_PHONE,
            "mnemonics": mnemonics_list
        }
        res = requests.post(f"{FRAGMENT_API_URL}/auth/authenticate/", json=payload)
        if res.status_code == 200:
            token = res.json().get("token")
            save_fragment_token(token)
            logger.info("✅ Успешная авторизация Fragment.")
            return token
        logger.error(f"❌ Ошибка авторизации Fragment: {res.text}")
        return None
    except Exception as e:
        logger.error(f"❌ Исключение при авторизации Fragment: {e}")
        return None


def check_username_exists(username):
    global FRAGMENT_TOKEN
    url = f"{FRAGMENT_API_URL}/misc/user/{username.lstrip('@')}/"
    headers = {
        "Accept": "application/json",
        "Authorization": f"JWT {FRAGMENT_TOKEN}"
    }
    try:
        res = requests.get(url, headers=headers)
        if res.status_code == 200:
            data = res.json()
            return "username" in data
        else:
            return False
    except Exception as e:
        logger.error(f"❌ Ошибка при проверке ника: {e}")
        return False


def direct_send_stars(token, username, quantity):
    try:
        data = {"username": username, "quantity": quantity}
        headers = {
            "Authorization": f"JWT {token}",
            "Content-Type": "application/json"
        }
        res = requests.post(f"{FRAGMENT_API_URL}/order/stars/", json=data, headers=headers)
        if res.status_code == 200:
            return True, res.text
        return False, res.text
    except Exception as e:
        return False, str(e)


def parse_fragment_error(response_text):
    try:
        data = json.loads(response_text)
    except:
        return "❌ Неизвестная ошибка."

    if isinstance(data, dict):
        if "username" in data:
            return "❌ Неверный Telegram-тег. Сейчас оформим возврат средств."
        if "quantity" in data:
            return "❌ Минимум 50 ⭐ для покупки. Сейчас оформим возврат средств."
        if "errors" in data:
            for err in data["errors"]:
                if "Not enough funds" in err.get("error", ""):
                    return "❌ Недостаточно средств у продавца для покупки. Сейчас оформим возврат средств."

    if isinstance(data, list):
        if any("Unknown error" in str(e) for e in data):
            return "❌ Неизвестная ошибка. Сейчас оформим возврат средств."

    return "❌ Ошибка обработки заказа."


def extract_stars_count(title: str) -> int:
    if not title:
        return 50
    title = title.lower()

    match = re.search(r"(?:зв[её]зд[а-я]*[^0-9]{0,10})?(\d{1,6})(?=\D*(зв|зв[её]зд|⭐|stars?))", title)
    if not match:
        match = re.search(r"(\d{1,6})\s*(зв|зв[её]зд|⭐|stars?)", title)
    if not match:
        match = re.search(r"(\d{1,6})", title)

    if match:
        count = int(match.group(1))
        return max(1, min(count, 1_000_000))

    return 50


def refund_order(account, order_id, chat_id):
    try:
        account.refund(order_id)
        logger.info(f"✔️ Возврат оформлен для заказа {order_id}")
        account.send_message(chat_id, "✅ Средства успешно возвращены.")
        return True
    except Exception as e:
        logger.error(f"❌ Не удалось вернуть средства за заказ {order_id}: {e}")
        account.send_message(chat_id, "❌ Ошибка возврата. Свяжитесь с админом.")
        return False


def get_subcategory_id_safe(order, account):
    subcat = getattr(order, "subcategory", None) or getattr(order, "sub_category", None)
    if subcat and hasattr(subcat, "id"):
        return subcat.id, subcat

    try:
        full_order = account.get_order(order.id)
        subcat = getattr(full_order, "subcategory", None) or getattr(full_order, "sub_category", None)
        if subcat and hasattr(subcat, "id"):
            return subcat.id, subcat
    except Exception as e:
        logger.warning(f"⚠️ Не удалось загрузить полный заказ: {e}")

    return None, None


def main():
    global FRAGMENT_TOKEN
    golden_key = os.getenv("FUNPAY_AUTH_TOKEN")
    if not golden_key:
        logger.error("❌ FUNPAY_AUTH_TOKEN не найден в .env")
        return

    account = Account(golden_key)
    account.get()

    if not account.username:
        logger.error("❌ Не удалось получить имя пользователя. Проверьте токен.")
        return

    logger.info(f"✅ Авторизован как {account.username}")
    runner = Runner(account)

    FRAGMENT_TOKEN = load_fragment_token() or authenticate_fragment()
    if not FRAGMENT_TOKEN:
        logger.error("❌ Не удалось авторизоваться в Fragment.")
        return

    logger.info("🤖 Бот запущен. Ожидание событий...")

    last_reply_time = 0

    for event in runner.listen(requests_delay=3.0):
        try:
            now = time.time()
            if now - last_reply_time < COOLDOWN_SECONDS:
                continue

            if isinstance(event, NewOrderEvent):
                subcat_id, subcat = get_subcategory_id_safe(event.order, account)
                if subcat_id != 2418:
                    logger.info(f"⏭ Пропуск заказа — не Telegram Stars (ID: {subcat_id or 'неизвестно'})")
                    continue

                logger.info(f"🔗 Лот: {subcat.public_link if subcat else '—'}")
                order = account.get_order(event.order.id)

                title = getattr(order, "title", None) or getattr(order, "short_description", None) \
                        or getattr(order, "full_description", None) or ""

                stars = extract_stars_count(title)

                logger.info(f"📦 Новый заказ: {title}")
                logger.info(f"💫 Извлечено звёзд: {stars}")

                buyer_id = order.buyer_id
                chat_id = order.chat_id

                waiting_for_nick[buyer_id] = {
                    "chat_id": chat_id,
                    "stars": stars,
                    "order_id": order.id,
                    "state": "awaiting_nick",
                    "temp_nick": None
                }

                account.send_message(chat_id, f"Спасибо за покупку!\nПожалуйста, отправьте ваш Telegram-тег (пример: @username), чтобы получить {stars} ⭐.")
                last_reply_time = now

            elif isinstance(event, NewMessageEvent):
                msg = event.message
                chat_id = msg.chat_id
                user_id = msg.author_id
                text = msg.text.strip()

                if user_id == account.id or user_id not in waiting_for_nick:
                    continue

                user_state = waiting_for_nick[user_id]
                stars = user_state["stars"]
                order_id = user_state["order_id"]

                if user_state["state"] == "awaiting_nick":
                    if not check_username_exists(text):
                        account.send_message(chat_id, f'❌ Ник "{text}" не найден. Пожалуйста, введите правильный Telegram-тег (пример: @username).')
                        last_reply_time = now
                        continue
                    else:
                        user_state["temp_nick"] = text
                        user_state["state"] = "awaiting_confirmation"
                        account.send_message(chat_id, f'Вы указали: "{text}". Если это ваш Telegram-тег, напишите "+", иначе отправьте другой.')
                        last_reply_time = now

                elif user_state["state"] == "awaiting_confirmation":
                    if text == "+":
                        username = user_state["temp_nick"].lstrip("@")
                        account.send_message(chat_id, f"🚀 Отправляю {stars} ⭐ пользователю @{username}...")
                        success, response = direct_send_stars(FRAGMENT_TOKEN, username, stars)

                        if success:
                            account.send_message(chat_id, f"✅ Успешно отправлено {stars} ⭐ пользователю @{username}!")
                            logger.info(f"✅ @{username} получил {stars} ⭐")
                        else:
                            short_error = parse_fragment_error(response)
                            account.send_message(chat_id, short_error + "\n🔁 Пытаюсь оформить возврат...")
                            refund_order(account, order_id, chat_id)

                        waiting_for_nick.pop(user_id)
                        last_reply_time = now
                    else:
                        if not check_username_exists(text):
                            account.send_message(chat_id, f'❌ Ник "{text}" не найден. Пожалуйста, введите правильный Telegram-тег.')
                        else:
                            user_state["temp_nick"] = text
                            account.send_message(chat_id, f'Вы указали: "{text}". Если это ваш Telegram-тег, напишите "+", иначе отправьте новый тег.')
                        last_reply_time = now

        except Exception as e:
            logger.error(f"❌ Ошибка обработки события: {e}")


if __name__ == "__main__":
    main()
