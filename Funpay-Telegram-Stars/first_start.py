from __future__ import annotations
import os
import sys
import json
import time
import shutil
import getpass
import datetime as dt
from typing import Dict, Optional, Tuple

try:
    from colorama import init as colorama_init, Fore, Style
    colorama_init(autoreset=True)
except Exception:
    class _Dummy: RESET_ALL = ""
    class _Fore(_Dummy): RED = GREEN = YELLOW = CYAN = MAGENTA = BLUE = WHITE = ""
    class _Style(_Dummy): BRIGHT = NORMAL = ""
    Fore, Style = _Fore(), _Style()

import requests

BACKUP_DIR = "backup_env"
ENV_PATH = ".env"
TOKEN_FILE = "auth_token.json"
FRAGMENT_API_URL = "https://api.fragment-api.com/v1"

DEFAULTS = {
    "FUNPAY_AUTH_TOKEN": "",
    "FRAGMENT_API_KEY": "",
    "FRAGMENT_PHONE": "",
    "FRAGMENT_MNEMONICS": "",
    "FRAGMENT_VERSION": "V4R2",
    "COOLDOWN_SECONDS": "1",
    "AUTO_REFUND": "false",
    "AUTO_DEACTIVATE": "false",
    "FRAGMENT_MIN_BALANCE": "5.0",
    "DEACTIVATE_CATEGORY_ID": "2418",
}

def info(msg: str): print(Fore.CYAN + msg + Style.RESET_ALL)
def ok(msg: str): print(Fore.GREEN + msg + Style.RESET_ALL)
def warn(msg: str): print(Fore.YELLOW + msg + Style.RESET_ALL)
def err(msg: str): print(Fore.RED + msg + Style.RESET_ALL)

def prompt_str(label: str, default: str = "", allow_empty: bool = False, secret: bool = False) -> str:
    while True:
        dpart = f" [{default}]" if default else ""
        val = (getpass.getpass if secret else input)(f"{label}{dpart}: ").strip()
        if not val and default:
            val = default
        if val or allow_empty:
            return val
        warn("Поле не может быть пустым.")

def prompt_bool(label: str, default: bool) -> bool:
    d = "Y/n" if default else "y/N"
    while True:
        ans = input(f"{label} ({d}): ").strip().lower()
        if not ans:
            return default
        if ans in ("y", "yes", "д", "да"):
            return True
        if ans in ("n", "no", "н", "нет"):
            return False
        warn("Ответьте 'y' или 'n'.")

def prompt_choice(label: str, choices: Tuple[str, ...], default: str) -> str:
    ch = "/".join(choices)
    while True:
        ans = input(f"{label} [{ch}] (по умолчанию {default}): ").strip().upper()
        if not ans:
            return default
        if ans in choices:
            return ans
        warn(f"Доступно: {', '.join(choices)}")

def prompt_float(label: str, default: float) -> float:
    while True:
        raw = input(f"{label} [{default}]: ").strip()
        if not raw:
            return default
        try:
            return float(raw.replace(",", "."))
        except Exception:
            warn("Введите число, например 5.0")

def load_env(path: str) -> Dict[str, str]:
    data: Dict[str, str] = {}
    if not os.path.exists(path):
        return data
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                data[k.strip()] = v.strip()
    except Exception as e:
        warn(f"Не удалось прочитать существующий {path}: {e}")
    return data

def backup_env(path: str):
    try:
        os.makedirs(BACKUP_DIR, exist_ok=True)
    except Exception as e:
        warn(f"Не удалось создать папку бэкапов {BACKUP_DIR}: {e}")

    if os.path.exists(path):
        ts = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        dst = os.path.join(BACKUP_DIR, f".env.backup-{ts}")
        try:
            shutil.copy2(path, dst)
            warn(f"Сделан бэкап {path} -> {dst}")
        except Exception as e:
            warn(f"Не удалось сделать бэкап {path}: {e}")

def save_env(path: str, data: Dict[str, str]):
    lines = []
    for k in DEFAULTS.keys():
        v = data.get(k, "")
        if "\n" in v or " " in v:
            v = v.replace('"', '\\"')
            v = f"\"{v}\""
        lines.append(f"{k}={v}")
    for k, v in data.items():
        if k not in DEFAULTS:
            lines.append(f"{k}={v}")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    ok(f"Сохранено в {path}")

def looks_like_golden_key(s: str) -> bool:
    return len(s) == 32

def normalize_phone(s: str) -> str:
    s = s.strip().replace(" ", "").replace("-", "")
    if s.startswith("+"):
        return "+" + "".join(ch for ch in s if ch.isdigit())
    return "".join(ch for ch in s if ch.isdigit())

def split_mnemonics(s: str) -> list[str]:
    return [w for w in s.strip().replace(",", " ").split() if w]

def validate_mnemonics(words: list[str]) -> bool:
    return 8 <= len(words) <= 30

def authenticate_fragment_now(api_key: str, phone: str, version: str, mnemonics_words: list[str]) -> Tuple[bool, str, Optional[str]]:
    payload = {
        "api_key": api_key,
        "phone_number": phone,
        "version": version,
        "mnemonics": mnemonics_words
    }
    try:
        r = requests.post(f"{FRAGMENT_API_URL}/auth/authenticate/", json=payload, timeout=60)
        try:
            j = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        except Exception:
            j = {}
        if r.status_code == 200 and isinstance(j, dict):
            token = j.get("token")
            if token:
                return True, "Успех (получен токен).", token
            return False, f"HTTP 200 без токена: {str(j)[:300]}", None
        return False, f"HTTP {r.status_code}: {r.text[:300]}", None
    except Exception as e:
        return False, f"Исключение: {e}", None

def maybe_write_fragment_token(token: str, version: str):
    try:
        with open(TOKEN_FILE, "w", encoding="utf-8") as f:
            json.dump({"token": token, "version": version, "ts": int(time.time())}, f, ensure_ascii=False)
        ok(f"Сохранён кэш токена в {TOKEN_FILE}")
    except Exception as e:
        warn(f"Не удалось сохранить {TOKEN_FILE}: {e}")

def try_login_funpay(golden_key: str) -> Tuple[bool, str]:
    try:
        from FunPayAPI import Account
        acc = Account(golden_key)
        acc.get()
        uname = getattr(acc, "username", None) or "(unknown)"
        return True, f"Авторизован как @{uname}"
    except Exception as e:
        return False, f"Ошибка входа: {e}"

def coerce_float(val, default) -> float:
    try:
        s = str(val).strip()
        if s == "":
            return float(default)
        s = s.replace(",", ".")
        return float(s)
    except Exception:
        return float(default)

def main():
    print(Style.BRIGHT + Fore.MAGENTA + "—" * 56 + Style.RESET_ALL)
    print(Style.BRIGHT + Fore.CYAN + "Добро пожаловать в первичную настройку StarsBot ✨" + Style.RESET_ALL)
    print(Style.BRIGHT + Fore.MAGENTA + "—" * 56 + Style.RESET_ALL)

    existing = load_env(ENV_PATH)
    if existing:
        warn("Найден существующий .env — значения будут предложены по умолчанию.\n")

    info("1) Данные FunPay")
    default_key = existing.get("FUNPAY_AUTH_TOKEN", DEFAULTS["FUNPAY_AUTH_TOKEN"])
    while True:
        golden_key = prompt_str("FUNPAY_AUTH_TOKEN (golden_key из EditThisCookie)", default=default_key)
        if looks_like_golden_key(golden_key):
            break
        warn("Похоже, формат не 32 символа. Если уверены — можно продолжить, но лучше перепроверить.")
        if not prompt_bool("Продолжить с таким значением?", True):
            continue
        else:
            break

    print()
    info("2) Данные Fragment")
    version = prompt_choice("Версия кошелька Fragment", ("V4R2", "W5"),
                            default=(existing.get("FRAGMENT_VERSION") or DEFAULTS["FRAGMENT_VERSION"]).upper())
    api_key = prompt_str("FRAGMENT_API_KEY", default=existing.get("FRAGMENT_API_KEY", DEFAULTS["FRAGMENT_API_KEY"]))
    phone = prompt_str("FRAGMENT_PHONE (пример: 227555777000 или +1227555777000)",
                       default=existing.get("FRAGMENT_PHONE", DEFAULTS["FRAGMENT_PHONE"]))
    phone = normalize_phone(phone)

    warn("Вставьте мнемонику (seed-phrase). Она будет сохранена в .env — храните файл в секрете.")
    mnemo_default = existing.get("FRAGMENT_MNEMONICS", DEFAULTS["FRAGMENT_MNEMONICS"]).replace('"', "").strip()
    mnemonics = prompt_str("FRAGMENT_MNEMONICS (слова через пробел)", default=mnemo_default, allow_empty=False)
    words = split_mnemonics(mnemonics)
    if not validate_mnemonics(words):
        warn(f"Необычное число слов ({len(words)}). Обычно 12 или 24.")

    print()
    info("3) Дополнительные настройки")
    cooldown_default = coerce_float(existing.get("COOLDOWN_SECONDS", DEFAULTS["COOLDOWN_SECONDS"]),
                                    DEFAULTS["COOLDOWN_SECONDS"])
    cooldown = prompt_float("COOLDOWN_SECONDS", cooldown_default)
    auto_refund = prompt_bool("Включить AUTO_REFUND (автовозврат при ошибках отправки)?",
                              (existing.get("AUTO_REFUND", DEFAULTS["AUTO_REFUND"]).lower() in ("1", "true", "yes", "y")))
    auto_deact = prompt_bool("Включить AUTO_DEACTIVATE (авто-выключение лотов при низком балансе)?",
                             (existing.get("AUTO_DEACTIVATE", DEFAULTS["AUTO_DEACTIVATE"]).lower() in ("1", "true", "yes", "y")))
    min_balance_default = coerce_float(existing.get("FRAGMENT_MIN_BALANCE", DEFAULTS["FRAGMENT_MIN_BALANCE"]),
                                       DEFAULTS["FRAGMENT_MIN_BALANCE"])
    min_balance = prompt_float("FRAGMENT_MIN_BALANCE (порог баланса для деактивации лотов)", min_balance_default)
    try:
        deactivate_cat = int(existing.get("DEACTIVATE_CATEGORY_ID", DEFAULTS["DEACTIVATE_CATEGORY_ID"]))
    except Exception:
        deactivate_cat = int(DEFAULTS["DEACTIVATE_CATEGORY_ID"])

    print()
    info("Проверьте введённые данные:")
    print(f"  FUNPAY_AUTH_TOKEN        = {mask(golden_key)}")
    print(f"  FRAGMENT_VERSION         = {version}")
    print(f"  FRAGMENT_API_KEY         = {mask(api_key)}")
    print(f"  FRAGMENT_PHONE           = {phone}")
    print(f"  FRAGMENT_MNEMONICS       = {mask(' '.join(words), head=2, tail=2)}")
    print(f"  COOLDOWN_SECONDS         = {cooldown}")
    print(f"  AUTO_REFUND              = {auto_refund}")
    print(f"  AUTO_DEACTIVATE          = {auto_deact}")
    print(f"  FRAGMENT_MIN_BALANCE     = {min_balance}")
    print(f"  DEACTIVATE_CATEGORY_ID   = {deactivate_cat}")
    print()

    if not prompt_bool("Сохранить в .env?", True):
        err("Отмена. Ничего не сохранено.")
        return

    backup_env(ENV_PATH)
    env_data = dict(existing)
    env_data.update({
        "FUNPAY_AUTH_TOKEN": golden_key,
        "FRAGMENT_API_KEY": api_key,
        "FRAGMENT_PHONE": phone,
        "FRAGMENT_MNEMONICS": " ".join(words),
        "FRAGMENT_VERSION": version,
        "COOLDOWN_SECONDS": str(cooldown),
        "AUTO_REFUND": "true" if auto_refund else "false",
        "AUTO_DEACTIVATE": "true" if auto_deact else "false",
        "FRAGMENT_MIN_BALANCE": str(min_balance),
        "DEACTIVATE_CATEGORY_ID": str(deactivate_cat),
    })
    save_env(ENV_PATH, env_data)

    print()
    if prompt_bool("Проверить авторизацию в Fragment сейчас?", True):
        ok_auth, msg, token = authenticate_fragment_now(api_key, phone, version, words)
        (ok if ok_auth else err)("Fragment: " + msg)
        if token:
            maybe_write_fragment_token(token, version)

    print()
    if prompt_bool("Проверить авторизацию в FunPay сейчас?", True):
        ok_fp, fp_msg = try_login_funpay(golden_key)
        (ok if ok_fp else err)("FunPay: " + fp_msg)

    print()
    ok("Готово! Теперь можно запускать основного бота.")

def mask(s: str, head: int = 3, tail: int = 3) -> str:
    if not s:
        return ""
    if len(s) <= head + tail:
        return "*" * len(s)
    return s[:head] + "*" * (len(s) - head - tail) + s[-tail:]

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print()
        warn("Прервано пользователем.")
