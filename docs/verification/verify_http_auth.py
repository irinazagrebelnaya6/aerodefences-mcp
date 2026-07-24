"""
Жива перевірка експлуатаційних вимог MCP-сервера (Частина 3 завдання).

Піднімає ОКРЕМИЙ, повністю контрольований екземпляр `server_aerodefences.py`
у HTTP-транспорті з власною згенерованою парою RSA-ключів (щоб самостійно
випускати валідні JWT), і послідовно демонструє:

  1. запуск сервера (лог старту);
  2. НЕавторизований запит → 401 (без токена і з невалідним токеном);
  3. АВТОРИЗОВАНИЙ MCP-клієнт (Bearer JWT, role=admin) → healthcheck/metrics/whoami;
  4. моніторинг: Prometheus-ендпоінт /metrics;
  5. завершення роботи (лог shutdown).

Запуск (з кореня репозиторію, поки піднята MySQL на 127.0.0.1:3307):
    .venv/bin/python docs/verification/verify_http_auth.py

Не чіпає вже розгорнутий compose-стек: слухає інші порти (8010 / 9110).
Логи сервера (JSON) пишуться у docs/verification/server.log.
"""

import asyncio
import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from fastmcp.server.auth.providers.jwt import RSAKeyPair

REPO = Path(__file__).resolve().parents[2]
OUT = Path(__file__).resolve().parent
SERVER_LOG = OUT / "server.log"

HTTP_PORT = 8010
METRICS_PORT = 9110
BASE = f"http://127.0.0.1:{HTTP_PORT}/mcp"
METRICS_URL = f"http://127.0.0.1:{METRICS_PORT}/metrics"
ISSUER = "https://demo.aerodefences.local"
AUDIENCE = "aerodefences-mcp"


def hr(title: str) -> None:
    print(f"\n{'=' * 70}\n{title}\n{'=' * 70}")


def http_status(url: str, token: str | None) -> int:
    """POST на MCP-ендпоінт; повертає HTTP-код (401 = відмова авторизації)."""
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}).encode()
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json, text/event-stream")
    if token is not None:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status
    except urllib.error.HTTPError as e:
        return e.code


def wait_ready(timeout: float = 30.0) -> bool:
    """Чекаємо, поки підніметься Prometheus-ендпоінт (ознака готовності)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(METRICS_URL, timeout=2) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            time.sleep(0.5)
    return False


async def authorized_client(token: str) -> dict:
    """Авторизований MCP-клієнт: викликає моніторингові інструменти."""
    transport = StreamableHttpTransport(
        BASE, headers={"Authorization": f"Bearer {token}"}
    )
    out = {}
    async with Client(transport) as client:
        out["whoami"] = (await client.call_tool("whoami", {})).data
        out["healthcheck"] = (await client.call_tool("healthcheck", {})).data
        out["metrics"] = (await client.call_tool("metrics", {})).data
    return out


def main() -> int:
    # 1) Пара ключів + JWT (роль admin у claim `role`).
    kp = RSAKeyPair.generate()
    token = kp.create_token(
        subject="demo-admin",
        issuer=ISSUER,
        audience=AUDIENCE,
        additional_claims={"role": "admin"},
        expires_in_seconds=3600,
    )

    # 2) Оточення дочірнього процесу. Порожній ADD_JWT_JWKS_URI перекриває
    #    успадкований .env, тож перевірка йде ЛИШЕ за нашим публічним ключем.
    env = dict(os.environ)
    env.update(
        ADD_TRANSPORT="http",
        ADD_HTTP_HOST="127.0.0.1",
        ADD_HTTP_PORT=str(HTTP_PORT),
        ADD_METRICS_PORT=str(METRICS_PORT),
        ADD_LOG_FORMAT="json",
        ADD_LOG_LEVEL="INFO",
        ADD_ROLE="viewer",  # deny-by-default; підвищення лише через JWT
        ADD_JWT_JWKS_URI="",
        ADD_JWT_PUBLIC_KEY=kp.public_key,
        ADD_JWT_ISSUER=ISSUER,
        ADD_JWT_AUDIENCE=AUDIENCE,
    )

    hr("КРОК 1. Запуск HTTP-сервера (окремий екземпляр, порти 8010/9110)")
    logf = open(SERVER_LOG, "w")
    proc = subprocess.Popen(
        [sys.executable, "server_aerodefences.py"],
        cwd=str(REPO), env=env, stdout=logf, stderr=subprocess.STDOUT,
    )
    try:
        if not wait_ready():
            print("СЕРВЕР НЕ ПІДНЯВСЯ — дивись server.log")
            return 1
        print(f"Сервер піднявся: MCP на :{HTTP_PORT}, метрики на :{METRICS_PORT}")

        hr("КРОК 2. Неавторизований запит → очікуємо 401")
        s_none = http_status(BASE, token=None)
        s_bad = http_status(BASE, token="invalid.token.value")
        print(f"  без токена            → HTTP {s_none}   {'✅' if s_none == 401 else '❌'}")
        print(f"  невалідний Bearer     → HTTP {s_bad}   {'✅' if s_bad == 401 else '❌'}")

        hr("КРОК 3. Авторизований клієнт (Bearer JWT, role=admin)")
        res = asyncio.run(authorized_client(token))
        print("  whoami      :", json.dumps(res["whoami"], ensure_ascii=False))
        print("  healthcheck :", json.dumps(res["healthcheck"], ensure_ascii=False))
        print("  metrics     :", json.dumps(res["metrics"], ensure_ascii=False))
        role_ok = res["whoami"].get("role") == "admin"
        db_ok = res["healthcheck"].get("status") == "ok"
        print(f"  роль з JWT = admin        {'✅' if role_ok else '❌'}")
        print(f"  healthcheck status = ok   {'✅' if db_ok else '⚠️  (БД недоступна для цього екземпляра)'}")

        hr("КРОК 4. Моніторинг: Prometheus /metrics")
        with urllib.request.urlopen(METRICS_URL, timeout=5) as resp:
            body = resp.read().decode()
        lines = [ln for ln in body.splitlines()
                 if ln.startswith("aerodefences_") and not ln.startswith("#")]
        print(f"  GET {METRICS_URL} → HTTP {resp.status}")
        for ln in lines:
            print("   ", ln)

        hr("КРОК 5. Завершення роботи (SIGTERM → лог shutdown)")
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        print(f"  процес завершено, код виходу: {proc.returncode}")
    finally:
        if proc.poll() is None:
            proc.kill()
        logf.close()

    hr("ЛОГИ СЕРВЕРА (docs/verification/server.log)")
    print(SERVER_LOG.read_text())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
