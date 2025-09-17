import os
import logging
import time
import json
import re
import requests
from typing import Optional, Tuple

from dotenv import load_dotenv
from FunPayAPI import Account
from FunPayAPI.updater.runner import Runner
from FunPayAPI.updater.events import NewOrderEvent, NewMessageEvent

# ============ ENV ============
load_dotenv()

COOLDOWN_SECONDS = float(os.getenv("COOLDOWN_SECONDS", "1"))
TOKEN_FILE = "auth_token.json"
FRAGMENT_API_URL = "https://api.fragment-api.com/v1"
waiting_for_nick: dict[int, dict] = {}

FRAGMENT_TOKEN: Optional[str] = None
FRAGMENT_API_KEY = os.getenv("FRAGMENT_API_KEY")
FRAGMENT_PHONE = os.getenv("FRAGMENT_PHONE")
FRAGMENT_MNEMONICS = os.getenv("FRAGMENT_MNEMONICS", "")
FRAGMENT_VERSION = (os.getenv("FRAGMENT_VERSION") or "V4R2").strip().upper()

DEACTIVATE_CATEGORY_ID = 2418

def _env_bool_raw(name: str):
    return os.getenv(name)

def _env_bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return str(v).strip().lower() in ("1", "true", "yes", "y")

AUTO_REFUND_RAW = _env_bool_raw("AUTO_REFUND")
AUTO_DEACTIVATE_RAW = _env_bool_raw("AUTO_DEACTIVATE")

AUTO_REFUND = _env_bool("AUTO_REFUND", False)
AUTO_DEACTIVATE = _env_bool("AUTO_DEACTIVATE", False)

FRAGMENT_MIN_BALANCE_RAW = os.getenv("FRAGMENT_MIN_BALANCE")
try:
    FRAGMENT_MIN_BALANCE = float(FRAGMENT_MIN_BALANCE_RAW) if FRAGMENT_MIN_BALANCE_RAW is not None else 5.0
except Exception:
    FRAGMENT_MIN_BALANCE = 5.0

# ============ COLORFUL + FILE LOGGING ============
try:
    from colorama import init as colorama_init, Fore, Style
    colorama_init(autoreset=True)
except Exception:
    class _Dummy: RESET_ALL = ""
    class _Fore(_Dummy): RED = GREEN = YELLOW = CYAN = MAGENTA = BLUE = WHITE = ""
    class _Style(_Dummy): BRIGHT = NORMAL = ""
    Fore, Style = _Fore(), _Style()

class ColorFormatter(logging.Formatter):
    LEVEL_COLORS = {
        logging.DEBUG: Fore.BLUE,
        logging.INFO: Fore.CYAN,
        logging.WARNING: Fore.YELLOW,
        logging.ERROR: Fore.RED,
        logging.CRITICAL: Fore.MAGENTA + Style.BRIGHT,
    }
    def format(self, record):
        color = self.LEVEL_COLORS.get(record.levelno, "")
        message = super().format(record)
        return f"{color}{message}{Style.RESET_ALL}"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s:%(lineno)d | %(message)s"
)
for h in logging.getLogger().handlers:
    try:
        h.setFormatter(ColorFormatter(h.formatter._fmt if hasattr(h, "formatter") and h.formatter else "%(message)s"))
    except Exception:
        pass

file_handler = logging.FileHandler("log.txt", encoding="utf-8")
file_handler.setLevel(logging.INFO)
file_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s:%(lineno)d | %(message)s"))
logging.getLogger().addHandler(file_handler)

logger = logging.getLogger("StarsBot")

def _excepthook(exc_type, exc, tb):
    logger.critical("UNHANDLED EXCEPTION", exc_info=(exc_type, exc, tb))
import sys
sys.excepthook = _excepthook

# ============ HELPERS ============
def _token_file_path() -> str:
    return TOKEN_FILE

def load_fragment_token() -> Optional[str]:
    p = _token_file_path()
    if not os.path.exists(p):
        return None
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return None
        token = data.get("token")
        saved_ver = (data.get("version") or "").strip().upper()
        if token and saved_ver and saved_ver != FRAGMENT_VERSION:
            logger.warning(Fore.YELLOW + f"[TOKEN] Версия токена {saved_ver} != текущей {FRAGMENT_VERSION}. "
                                         f"Пробую с кэшем; при 401/403 выполню переавторизацию.")
        return token
    except Exception as e:
        logger.debug(f"Не удалось прочитать {p}: {e}")
        return None

def save_fragment_token(token: str):
    p = _token_file_path()
    try:
        with open(p, "w", encoding="utf-8") as f:
            json.dump({"token": token, "version": FRAGMENT_VERSION, "ts": int(time.time())}, f, ensure_ascii=False)
    except Exception as e:
        logger.warning(f"Не удалось сохранить токен в {p}: {e}")

def authenticate_fragment() -> Optional[str]:
    try:
        mnemonics_list = [w for w in FRAGMENT_MNEMONICS.strip().split() if w]
        payload = {
            "api_key": FRAGMENT_API_KEY,
            "phone_number": FRAGMENT_PHONE,
            "version": FRAGMENT_VERSION,
            "mnemonics": mnemonics_list
        }
        res = requests.post(f"{FRAGMENT_API_URL}/auth/authenticate/", json=payload, timeout=60)
        if res.status_code == 200:
            token = res.json().get("token")
            save_fragment_token(token)
            logger.info(Fore.GREEN + f"✅ Успешная авторизация Fragment (version={FRAGMENT_VERSION}).")
            return token
        logger.error(Fore.RED + f"❌ Ошибка авторизации Fragment [{res.status_code}]: {res.text}")
        return None
    except Exception as e:
        logger.exception(Fore.RED + f"❌ Исключение при авторизации Fragment: {e}")
        return None

def fragment_request(method: str, path: str, retry_on_auth: bool = True, **kwargs) -> requests.Response:
    global FRAGMENT_TOKEN
    url = f"{FRAGMENT_API_URL}{path}"
    headers = kwargs.pop("headers", {}) or {}
    headers.setdefault("Accept", "application/json")
    if "json" in kwargs:
        headers.setdefault("Content-Type", "application/json")
    if FRAGMENT_TOKEN:
        headers["Authorization"] = f"JWT {FRAGMENT_TOKEN}"
    timeout = kwargs.pop("timeout", 10)

    try:
        r = requests.request(method, url, headers=headers, timeout=timeout, **kwargs)
        if r.status_code in (401, 403) and retry_on_auth:
            logger.info(Fore.YELLOW + "🔑 Токен недействителен. Переавторизация…")
            FRAGMENT_TOKEN = authenticate_fragment()
            if FRAGMENT_TOKEN:
                headers["Authorization"] = f"JWT {FRAGMENT_TOKEN}"
                r = requests.request(method, url, headers=headers, timeout=timeout, **kwargs)
        return r
    except Exception as e:
        logger.error(Fore.RED + f"❌ Ошибка HTTP-запроса к Fragment {method} {path}: {e}")
        raise

def check_username_exists(username: str) -> bool:
    uname = username.lstrip('@').strip()
    try:
        r = fragment_request("GET", f"/misc/user/{uname}/", timeout=8)
        ok = (r.status_code == 200 and isinstance(r.json(), dict) and "username" in r.json())
        if not ok:
            logger.info(f"[USERCHECK] {uname}: HTTP {r.status_code} | {r.text[:500]}")
        return ok
    except Exception as e:
        logger.error(Fore.RED + f"❌ Ошибка при проверке ника @{uname}: {e}")
        return False

def direct_send_stars(username: str, quantity: int) -> Tuple[bool, str, int]:
    data = {"username": username, "quantity": quantity}
    try:
        r = fragment_request("POST", "/order/stars/", json=data, timeout=60)
        return (r.status_code == 200, r.text, r.status_code)
    except Exception as e:
        return (False, str(e), 0)

def parse_fragment_error(response_text: str, status_code: int = 0) -> str:
    fallback = "Ошибка обработки заказа."
    try:
        data = json.loads(response_text)
    except Exception:
        data = None

    if status_code == 429:
        return "Слишком много запросов. Попробуйте ещё раз через минуту."
    if status_code in (500, 502, 503, 504):
        return "Сервис Fragment временно недоступен. Повторите позже."
    if status_code in (401, 403):
        return "Нужна повторная авторизация. Попробуйте ещё раз (мы уже переавторизуемся)."

    if isinstance(data, dict):
        if "username" in data:
            return "Неверный Telegram-тег (проверьте @username)."
        if "quantity" in data:
            return "Минимальное количество для покупки — 50 ⭐."
        for k in ("detail", "message", "error"):
            if data.get(k):
                msg = str(data[k])
                if "Not enough" in msg or "balance" in msg.lower():
                    return "Недостаточно средств на кошельке Fragment."
                if "version" in msg.lower():
                    return "Неверная версия кошелька. Проверьте FRAGMENT_VERSION."
                if "username" in msg.lower():
                    return "Пользователь с таким @username не найден."
                return msg[:200]
        if isinstance(data.get("errors"), list):
            joined = " | ".join(str(x.get("error") or x) for x in data["errors"][:3])
            if "balance" in joined.lower():
                return "Недостаточно средств на кошельке Fragment."
            return (joined or fallback)[:200]
        if isinstance(data.get("data"), dict):
            inner = data["data"]
            for k in ("error", "message", "detail"):
                if inner.get(k):
                    return str(inner[k])[:200]
    elif isinstance(data, list) and data:
        txt = " | ".join(str(x) for x in data[:3])
        return txt[:200]

    return fallback

def extract_stars_count(title: str, description: str = "") -> int:
    text = f"{title or ''} {description or ''}".lower()
    match = re.search(r"tg_stars[:=]\s*(\d{1,6})", text)
    if match:
        return int(match.group(1))
    match = re.search(r"(?:зв[её]зд[а-я]*[^0-9]{0,10})?(\d{1,6})(?=\D*(зв|зв[её]зд|⭐|stars?))", text)
    if not match:
        match = re.search(r"(\d{1,6})\s*(зв|зв[её]зд|⭐|stars?)", text)
    if not match:
        match = re.search(r"(\d{1,6})", text)
    return int(match.group(1)) if match else 50

def refund_order(account, order_id, chat_id, reason: str = ""):
    try:
        account.refund(order_id)
        logger.warning(Fore.YELLOW + f"↩️ Возврат оформлен для заказа {order_id}. Причина: {reason}")
        if chat_id:
            account.send_message(chat_id, "✅ Средства успешно возвращены.")
        return True
    except Exception as e:
        logger.error(Fore.RED + f"❌ Не удалось вернуть средства за заказ {order_id}: {e}")
        if chat_id:
            account.send_message(chat_id, "❌ Ошибка возврата. Свяжитесь с админом.")
        return False

def log_order_api_error(order_id, api_response_text, short_error, status_code: int = 0):
    logger.error(Fore.RED + f"{order_id} | HTTP {status_code} | {str(api_response_text)[:800]} | {short_error}")

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

def check_fragment_balance() -> Optional[float]:
    try:
        r = fragment_request("GET", "/misc/wallet/", timeout=8)
    except Exception:
        return None

    logger.debug(Fore.CYAN + f"[BALANCE] HTTP {r.status_code} | body: {r.text[:1000]}")
    if r.status_code != 200:
        logger.warning(Fore.YELLOW + f"[BALANCE] Некорректный ответ {r.status_code} при запросе баланса Fragment")
        return None

    try:
        data = r.json()
    except Exception as e:
        logger.error(Fore.RED + f"[BALANCE] Не удалось распарсить JSON от Fragment: {e}")
        return None

    if isinstance(data, dict):
        for k in ("balance", "amount", "wallet_balance", "available_balance"):
            if k in data:
                try:
                    return float(data[k])
                except Exception:
                    pass
        if "wallet" in data and isinstance(data["wallet"], dict):
            for k in ("balance", "amount", "available"):
                if k in data["wallet"]:
                    try:
                        return float(data["wallet"][k])
                    except Exception:
                        pass
        if "data" in data and isinstance(data["data"], dict):
            for k in ("balance", "amount"):
                if k in data["data"]:
                    try:
                        return float(data["data"][k])
                    except Exception:
                        pass

    logger.warning(Fore.YELLOW + f"[BALANCE] Не удалось извлечь баланс из ответа Fragment: {json.dumps(data)[:2000]}")
    return None

def deactivate_category(account: Account, category_id: int):
    deactivated = 0
    my_lots = None

    candidates = [
        ("get_my_subcategory_lots", lambda cid: account.get_my_subcategory_lots(cid)),
        ("get_my_lots", lambda cid: account.get_my_lots(cid) if hasattr(account, "get_my_lots") else None),
        ("get_my_lots_all", lambda cid: account.get_my_lots() if hasattr(account, "get_my_lots") else None),
    ]

    for name, fn in candidates:
        try:
            res = fn(category_id)
            if res:
                my_lots = res
                logger.debug(Fore.CYAN + f"[LOTS] Получили лоты через {name}, count={len(res) if hasattr(res,'__len__') else 'unknown'}")
                break
        except Exception as e:
            logger.debug(Fore.YELLOW + f"[LOTS] Метод {name} выбросил исключение: {e}")

    if my_lots is None:
        logger.error(Fore.RED + f"[LOTS] Не удалось получить список лотов для категории {category_id} (проверь API FunPay).")
        return 0

    for lot in my_lots:
        lot_id = None
        if hasattr(lot, "id"):
            lot_id = getattr(lot, "id")
        elif isinstance(lot, dict) and "id" in lot:
            lot_id = lot["id"]
        else:
            if isinstance(lot, dict):
                lot_id = lot.get("lot_id") or lot.get("id")
        if not lot_id:
            logger.debug(Fore.YELLOW + f"[LOTS] Пропускаем элемент без id: {lot}")
            continue

        field = None
        get_field_methods = ["get_lot_fields", "get_lot_field", "get_lot", "get_lot_by_id"]
        for fn_name in get_field_methods:
            try:
                fn = getattr(account, fn_name, None)
                if callable(fn):
                    field = fn(lot_id)
                    if field:
                        logger.debug(Fore.CYAN + f"[LOTS] Получили поля лота {lot_id} через {fn_name}")
                        break
            except Exception as e:
                logger.debug(Fore.YELLOW + f"[LOTS] {fn_name}({lot_id}) выбросил: {e}")
                field = None

        if not field:
            logger.warning(Fore.YELLOW + f"[LOTS] Не удалось получить поля лота {lot_id}. Пропускаем.")
            continue

        try:
            if isinstance(field, dict):
                field["active"] = False
            else:
                try:
                    setattr(field, "active", False)
                except Exception:
                    if hasattr(field, "is_active"):
                        try:
                            setattr(field, "is_active", False)
                        except Exception:
                            pass
        except Exception as e:
            logger.debug(Fore.YELLOW + f"[LOTS] Не удалось установить active=False для лота {lot_id}: {e}")

        saved = False
        save_methods = ["save_lot", "save_lot_field", "update_lot", "update_lot_field"]
        for sm in save_methods:
            try:
                fn = getattr(account, sm, None)
                if callable(fn):
                    fn(field)
                    saved = True
                    logger.info(Fore.YELLOW + f"[LOTS] Деактивирован лот {lot_id} через {sm}")
                    deactivated += 1
                    break
            except Exception as e:
                logger.debug(Fore.YELLOW + f"[LOTS] {sm} для {lot_id} выбросил: {e}")

        if not saved:
            try:
                account.save_lot(field)
                saved = True
                logger.info(Fore.YELLOW + f"[LOTS] Деактивирован лот {lot_id} через fallback save_lot")
                deactivated += 1
            except Exception as e:
                logger.error(Fore.RED + f"[LOTS] Не удалось деактивировать лот {lot_id}: {e}")

    logger.warning(Fore.YELLOW + f"[LOTS] Всего деактивировано: {deactivated}")
    return deactivated

# ============ MAIN LOOP ============
def main():
    global FRAGMENT_TOKEN
    golden_key = os.getenv("FUNPAY_AUTH_TOKEN")
    if not golden_key:
        logger.error(Fore.RED + "❌ FUNPAY_AUTH_TOKEN не найден в .env")
        return

    if FRAGMENT_VERSION not in ("V4R2", "W5"):
        logger.warning(Fore.YELLOW + f"⚠️ Неизвестная FRAGMENT_VERSION={FRAGMENT_VERSION}. Разрешены: V4R2, W5.")

    account = Account(golden_key)
    account.get()

    if AUTO_REFUND_RAW is None:
        logger.warning(Fore.YELLOW + "⚠️ AUTO_REFUND не задан в .env (по умолчанию выключен). Чтобы включить: AUTO_REFUND=true")
    if AUTO_DEACTIVATE_RAW is None:
        logger.warning(Fore.YELLOW + "⚠️ AUTO_DEACTIVATE не задан в .env (по умолчанию выключен). Чтобы включить: AUTO_DEACTIVATE=true")
    if FRAGMENT_MIN_BALANCE_RAW is None:
        logger.warning(Fore.YELLOW + f"⚠️ FRAGMENT_MIN_BALANCE не задан в .env. Используется дефолт {FRAGMENT_MIN_BALANCE}")

    logger.info(Fore.GREEN + f"🔐 Авторизован как {getattr(account, 'username', '(unknown)')}")
    logger.info(Fore.CYAN + f"Настройки: AUTO_REFUND={AUTO_REFUND}, AUTO_DEACTIVATE={AUTO_DEACTIVATE}, "
                            f"FRAGMENT_MIN_BALANCE={FRAGMENT_MIN_BALANCE}, DEACTIVATE_CATEGORY_ID={DEACTIVATE_CATEGORY_ID}, "
                            f"FRAGMENT_VERSION={FRAGMENT_VERSION}")

    runner = Runner(account)

    FRAGMENT_TOKEN = load_fragment_token() or authenticate_fragment()
    if not FRAGMENT_TOKEN:
        logger.error(Fore.RED + "❌ Не удалось авторизоваться в Fragment.")
        return

    logger.info(Style.BRIGHT + Fore.WHITE + "🚀 StarsBot запущен. Ожидание событий...")

    last_reply_time = 0.0

    for event in runner.listen(requests_delay=3.0):
        try:
            now = time.time()
            if now - last_reply_time < COOLDOWN_SECONDS:
                continue

            if isinstance(event, NewOrderEvent):
                subcat_id, subcat = get_subcategory_id_safe(event.order, account)
                if subcat_id != 2418:
                    logger.info(Fore.BLUE + f"⏭ Пропуск заказа — не Telegram Stars (ID: {subcat_id or 'неизвестно'})")
                    continue

                order = account.get_order(event.order.id)
                title = getattr(order, "title", "") or getattr(order, "short_description", "") or getattr(order, "full_description", "") or ""
                desc = getattr(order, "full_description", "") or getattr(order, "short_description", "") or ""
                stars = extract_stars_count(title, desc)

                logger.info(Style.BRIGHT + Fore.WHITE + "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
                logger.info(Fore.CYAN + f"🆕 Новый заказ #{order.id}")
                logger.info(Fore.CYAN + f"📦 Товар: {title}")
                logger.info(Fore.MAGENTA + f"💫 Извлечено звёзд: {stars}")
                logger.info(Style.BRIGHT + Fore.WHITE + "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")

                buyer_id, chat_id = order.buyer_id, order.chat_id
                waiting_for_nick[buyer_id] = {"chat_id": chat_id, "stars": stars, "order_id": order.id, "state": "awaiting_nick", "temp_nick": None}
                msg_after_purchase = f"""🎉 Спасибо за покупку!

                К выдаче: {stars} звезд⭐

                Пожалуйста, пришлите ваш Telegram-тег в формате @username.
                Если не знаете свой тег: откройте профиль Telegram → «Имя пользователя».

                После отправки тега я попрошу вас его подтвердить."""
                account.send_message(chat_id, msg_after_purchase)
                last_reply_time = now

            elif isinstance(event, NewMessageEvent):
                msg, chat_id, user_id = event.message, event.message.chat_id, event.message.author_id
                text = (event.message.text or "").strip()
                if user_id == account.id or user_id not in waiting_for_nick:
                    continue

                user_state = waiting_for_nick[user_id]
                stars, order_id = user_state["stars"], user_state["order_id"]

                if user_state["state"] == "awaiting_nick":
                    if not check_username_exists(text):
                        account.send_message(chat_id, f'❌ Ник "{text}" не найден. Пожалуйста, введите правильный Telegram-тег (пример: @username).')
                        last_reply_time = now
                        continue
                    else:
                        user_state["temp_nick"] = text
                        user_state["state"] = "awaiting_confirmation"
                        account.send_message(
                            chat_id,
                            f"⁡Вы указали: {text}.\nЕсли верно — отправьте +.\nЕсли нужно изменить — пришлите другой тег в формате @username."
                        )
                        last_reply_time = now

                elif user_state["state"] == "awaiting_confirmation":
                    if text == "+":
                        username = user_state["temp_nick"].lstrip("@")
                        account.send_message(chat_id, f"🚀 Отправляю {stars} ⭐ пользователю @{username}...")
                        success, response, status_code = direct_send_stars(username, stars)
                        if success:
                            account.send_message(chat_id, f"✅ Успешно отправлено {stars} ⭐ пользователю @{username}!")
                            logger.info(Fore.GREEN + f"✅ @{username} получил {stars} ⭐ (order {order_id})")

                            order_url = f"https://funpay.com/orders/{order_id}/"
                            account.send_message(
                                chat_id,
                                "🙏 Пожалуйста, подтвердите выполнение заказа и оставьте отзыв — это очень помогает!\n"
                                f"Ссылка на заказ: {order_url}"
                            )
                        else:
                            short_error = parse_fragment_error(response, status_code=status_code)
                            log_order_api_error(order_id, response, short_error, status_code=status_code)

                            if AUTO_REFUND:
                                account.send_message(chat_id, short_error + "\n🔁 Пытаюсь оформить возврат…")
                                refunded = refund_order(account, order_id, chat_id, reason=short_error)
                                if not refunded:
                                    notify_text = f"Не удалось автоматически вернуть средства по заказу {order_id}. Причина: {short_error}"
                                    logger.warning(Fore.MAGENTA + notify_text)
                            else:
                                account.send_message(chat_id, short_error + "\n⚠️ Автоматический возврат отключён. Свяжитесь с админом для возврата.")
                                logger.warning(Fore.MAGENTA + f"Авто-возврат отключён. Заказ {order_id} требует ручного возврата. Причина: {short_error}")

                            balance = check_fragment_balance()
                            if balance is not None:
                                logger.info(Fore.MAGENTA + f"[BALANCE] Текущий баланс Fragment: {balance}")
                                if balance < FRAGMENT_MIN_BALANCE:
                                    logger.warning(Fore.YELLOW + f"[BALANCE] Баланс Fragment {balance} < порога {FRAGMENT_MIN_BALANCE}")
                                    if AUTO_DEACTIVATE:
                                        deactivated = deactivate_category(account, DEACTIVATE_CATEGORY_ID)
                                        logger.warning(Fore.MAGENTA + f"Авто-деактивировано {deactivated} лотов в подкатегории {DEACTIVATE_CATEGORY_ID}")
                                    else:
                                        logger.warning(Fore.MAGENTA + f"AUTO_DEACTIVATE отключён — требуется ручная деактивация лотов (подкатегория {DEACTIVATE_CATEGORY_ID}).")
                            else:
                                logger.warning(Fore.YELLOW + "[BALANCE] Не удалось определить баланс Fragment (endpoint /misc/wallet/ вернул некорректный формат).")

                        waiting_for_nick.pop(user_id, None)
                        last_reply_time = now
                    else:
                        if not check_username_exists(text):
                            account.send_message(chat_id, f'❌ Ник "{text}" не найден. Пожалуйста, введите правильный Telegram-тег.')
                        else:
                            user_state["temp_nick"] = text
                            account.send_message(
                                chat_id,
                                f"⁡Вы указали: {text}.\nЕсли верно — отправьте +.\nЕсли нужно изменить — пришлите другой тег в формате @username."
                            )
                        last_reply_time = now

        except Exception as e:
            logger.exception(Fore.RED + f"❌ Ошибка обработки события: {e}")

if __name__ == "__main__":
    main()
