"""
Конфігурація та «каркас» сервера AeroDefences MCP.

Тут зібрано те, що не належить жодному конкретному інструменту: читання env,
константи, рівні ролей (RBAC), налаштування логування, побудова JWT-authn і
сам екземпляр `mcp = FastMCP(...)`, на якому реєструються всі інструменти,
ресурси та prompt-и (див. модулі `ad_tools_*`, `ad_resources`, `ad_prompts`).
"""

import json
import logging
import os

from dotenv import load_dotenv

# Читаємо .env (якщо є) ще до формування будь-яких конфігів (DB_CONFIG у ad_db).
load_dotenv()

from fastmcp import FastMCP
from fastmcp.server.auth.providers.jwt import JWTVerifier

ALLOWED_STATUSES = ("draft", "published", "archived")

# ── Безпека: рівні ролей (RBAC) ─────────────────────────────────────────
# viewer  — лише читання (read-інструменти);
# editor  — читання + звичайні write-операції (ціна, склад, тексти, FAQ);
# admin   — усе, включно з compliance-прапорцями та масовими діями.
ROLES = {"viewer": 0, "editor": 1, "admin": 2}

# Стеля на розмір вибірки read-інструментів (захист від «витягни весь каталог»).
MAX_LIMIT = int(os.getenv("ADD_MAX_LIMIT", "200"))

# Поля products, які дозволено редагувати через update_product_field.
# БІЛИЙ СПИСОК: лише текстові поля. slug/sku/status/price/stock мають окремі
# інструменти й сюди свідомо НЕ входять.
ALLOWED_UPDATE_FIELDS = (
    "subtitle", "short_description", "long_description", "key_advantage",
    "package_contents", "sku_descriptor", "documentation_url",
    "highlights_intro", "specs_intro", "built_for_intro", "compatible_intro",
)

# ── Стан сесії: «кошик вибраних продуктів» ──────────────────────────────
SELECTION_KEY = "selected_products"
SELECTION_TTL = int(os.getenv("ADD_SELECTION_TTL", "3600"))  # сек, лише для Redis

# Транспорт визначаємо на рівні модуля: від нього залежить authn і джерело ролі.
TRANSPORT = os.getenv("ADD_TRANSPORT", "stdio")


class _JsonLogFormatter(logging.Formatter):
    """Структурований JSON-рядок на лог-запис (для прод-збирача: Loki/ELK)."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


# Логи йдуть у stderr (не заважає stdio-протоколу MCP у stdout).
# ADD_LOG_LEVEL=DEBUG|INFO|WARNING керує детальністю;
# ADD_LOG_FORMAT=json|text — формат (json для проду, text для локальної розробки).
_log_handler = logging.StreamHandler()
if os.getenv("ADD_LOG_FORMAT", "text").lower() == "json":
    _log_handler.setFormatter(_JsonLogFormatter())
else:
    _log_handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s aerodefences %(message)s")
    )
logging.basicConfig(level=os.getenv("ADD_LOG_LEVEL", "INFO"), handlers=[_log_handler])
log = logging.getLogger("aerodefences")


def _build_auth():
    """JWT-автентифікація для мережевого (HTTP) транспорту.

    stdio (локальний довірений хост) authn не потребує → None.
    Для http вимагаємо джерело ключів (JWKS-URL або публічний ключ): без нього
    сервер НЕ стартує, щоб не підняти незахищений мережевий ендпоінт (fail-safe).
    """
    if TRANSPORT != "http":
        return None
    jwks_uri = os.getenv("ADD_JWT_JWKS_URI")
    public_key = os.getenv("ADD_JWT_PUBLIC_KEY")
    if not (jwks_uri or public_key):
        raise RuntimeError(
            "HTTP-транспорт вимагає JWT-authn: задай ADD_JWT_JWKS_URI або ADD_JWT_PUBLIC_KEY."
        )
    return JWTVerifier(
        jwks_uri=jwks_uri,
        public_key=public_key,
        issuer=os.getenv("ADD_JWT_ISSUER"),
        audience=os.getenv("ADD_JWT_AUDIENCE"),
    )


mcp = FastMCP(name="AeroDefences Catalog Server", auth=_build_auth())
