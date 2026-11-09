"""Вход в Яндекс Вебмастер и Google Search Console.

Разрешение пользователь подтверждает сам в браузере. Токены лежат только
в data/tokens.json с доступом для владельца файла и никуда не выводятся.
"""
from __future__ import annotations

import base64
import getpass
import hashlib
import json
import os
import re
import secrets
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx

TOKENS_PATH = Path("data/tokens.json")
GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_SCOPE = "https://www.googleapis.com/auth/webmasters.readonly"
WEBMASTER_USER_URL = "https://api.webmaster.yandex.net/v4/user"
METRIKA_COUNTERS_URL = "https://api-metrika.yandex.net/management/v1/counters"
METRIKA_SCOPE = "metrika:read"


class AuthError(RuntimeError):
    pass


def load_tokens(path: Path = TOKENS_PATH) -> dict:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def save_token(provider: str, data: dict, path: Path = TOKENS_PATH) -> None:
    tokens = load_tokens(path)
    tokens[provider] = data
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(tokens, handle)
    os.chmod(path, 0o600)


def yandex_token(path: Path = TOKENS_PATH) -> str:
    entry = load_tokens(path).get("yandex")
    if not entry:
        raise AuthError("Нет входа в Вебмастер. Выполните: seo auth yandex --client-id <ClientID>")
    return entry["access_token"]


def login_yandex(client_id: str, client: httpx.Client | None = None) -> None:
    """Токен выдаётся на странице Яндекса и вводится в скрытое поле терминала."""
    url = "https://oauth.yandex.ru/authorize?" + urlencode({"response_type": "token", "client_id": client_id})
    print(f"Откройте и подтвердите доступ: {url}")
    webbrowser.open(url)
    token = getpass.getpass("Вставьте токен со страницы Яндекса (ввод не отображается): ").strip()
    http = client or httpx.Client(timeout=30)
    response = http.get(WEBMASTER_USER_URL, headers={"Authorization": f"OAuth {token}"})
    if response.status_code != 200:
        raise AuthError(f"Яндекс не принял токен: HTTP {response.status_code}")
    save_token("yandex", {"access_token": token, "saved_at": int(time.time())})
    print("Готово: вход в Вебмастер сохранён.")


def extract_token(raw: str) -> str:
    """Принять и сам токен, и скопированный адрес страницы с access_token."""
    match = re.search(r"access_token=([^&\s#]+)", raw)
    if match:
        return match.group(1)
    token = raw.strip().strip("\"'")
    if not re.fullmatch(r"[\w.\-]+", token, re.ASCII):
        raise AuthError("Вставлен не токен: скопируйте только строку токена со страницы Яндекса")
    return token


def metrika_token(path: Path = TOKENS_PATH) -> str:
    entry = load_tokens(path).get("metrika")
    if not entry:
        raise AuthError("Нет входа в Метрику. Выполните: seo auth metrika --client-id <ClientID>")
    return entry["access_token"]


def login_metrika(client_id: str, client: httpx.Client | None = None) -> None:
    """Токен запрашивается только на чтение, даже если приложению доступна запись."""
    url = "https://oauth.yandex.ru/authorize?" + urlencode({
        "response_type": "token", "client_id": client_id, "scope": METRIKA_SCOPE,
    })
    print(f"Откройте и подтвердите доступ: {url}")
    webbrowser.open(url)
    token = extract_token(getpass.getpass("Вставьте токен со страницы Яндекса (ввод не отображается): "))
    http = client or httpx.Client(timeout=30)
    response = http.get(METRIKA_COUNTERS_URL, headers={"Authorization": f"OAuth {token}"})
    if response.status_code != 200:
        raise AuthError(f"Метрика не приняла токен: HTTP {response.status_code}: {response.text[:200]}")
    save_token("metrika", {"access_token": token, "saved_at": int(time.time())})
    print("Готово: вход в Метрику сохранён.")


def wait_for_code(server: HTTPServer, state: str, timeout: float = 300) -> str:
    """Принять один редирект Google на локальный адрес и проверить state."""
    result: dict[str, str] = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            query = parse_qs(urlsplit(self.path).query)
            if query.get("state", [""])[0] != state:
                result["error"] = "state"
            elif "code" in query:
                result["code"] = query["code"][0]
            else:
                result["error"] = query.get("error", ["unknown"])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write("Можно закрыть эту вкладку и вернуться к терминалу.".encode())

        def log_message(self, *args):
            pass

    server.RequestHandlerClass = Handler
    server.timeout = 1
    deadline = time.monotonic() + timeout
    while not result and time.monotonic() < deadline:
        server.handle_request()
    if "code" in result:
        return result["code"]
    raise AuthError(f"Google не выдал код: {result.get('error', 'истекло время ожидания')}")


def login_google(client_file: str | Path, client: httpx.Client | None = None) -> None:
    config = json.loads(Path(client_file).read_text(encoding="utf-8"))
    app = config.get("installed") or config.get("web")
    if not app:
        raise AuthError("Нужен JSON-файл клиента OAuth типа Desktop app")
    server = HTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler)
    redirect = f"http://127.0.0.1:{server.server_port}/"
    state = secrets.token_urlsafe(24)
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    url = GOOGLE_AUTH_URL + "?" + urlencode({
        "client_id": app["client_id"], "redirect_uri": redirect, "response_type": "code",
        "scope": GOOGLE_SCOPE, "access_type": "offline", "prompt": "consent", "state": state,
        "code_challenge": challenge, "code_challenge_method": "S256",
    })
    print(f"Откройте и подтвердите доступ: {url}")
    webbrowser.open(url)
    try:
        code = wait_for_code(server, state)
    finally:
        server.server_close()
    http = client or httpx.Client(timeout=30)
    response = http.post(GOOGLE_TOKEN_URL, data={
        "client_id": app["client_id"], "client_secret": app.get("client_secret", ""), "code": code,
        "code_verifier": verifier, "grant_type": "authorization_code", "redirect_uri": redirect,
    })
    if response.status_code != 200:
        raise AuthError(f"Google не выдал токен: HTTP {response.status_code}")
    payload = response.json()
    if "refresh_token" not in payload:
        raise AuthError("Google не вернул refresh_token, повторите вход")
    save_token("google", {"refresh_token": payload["refresh_token"], "client_id": app["client_id"],
                          "client_secret": app.get("client_secret", ""), "saved_at": int(time.time())})
    print("Готово: вход в Search Console сохранён.")


def google_access_token(client: httpx.Client, path: Path = TOKENS_PATH) -> str:
    entry = load_tokens(path).get("google")
    if not entry:
        raise AuthError("Нет входа в Search Console. Выполните: seo auth google --client-file <файл>")
    response = client.post(GOOGLE_TOKEN_URL, data={
        "client_id": entry["client_id"], "client_secret": entry["client_secret"],
        "refresh_token": entry["refresh_token"], "grant_type": "refresh_token",
    })
    if response.status_code != 200:
        raise AuthError(f"Не удалось обновить токен Google: HTTP {response.status_code}. Войдите заново.")
    return response.json()["access_token"]


def run_callback_for_test(server: HTTPServer, state: str) -> tuple[threading.Thread, dict]:
    box: dict = {}

    def target():
        try:
            box["code"] = wait_for_code(server, state, timeout=10)
        except AuthError as exc:
            box["error"] = str(exc)

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    return thread, box
