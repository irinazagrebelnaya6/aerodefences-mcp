"""
Конфігурація та «каркас» сервера AeroDefences MCP.

Env-налаштування, які зчитуються ОДИН раз на старті, згруповано у frozen
`@dataclass Config` (єдина точка читання env) + синглтон `config`. Динамічні
значення (роль `ADD_ROLE`/`ADD_DEV_ROLE`, `ADD_REDIS_URL`) свідомо читаються при
виклику — щоб їх можна було міняти в рантаймі/тестах, тож вони НЕ в Config.

Тут також константи (RBAC-рівні, дозволені статуси/поля), налаштування
логування, побудова JWT-authn і сам екземпляр `mcp = FastMCP(...)`, на якому
реєструються інструменти/ресурси/prompt-и (див. `ad_tools_*`, `ad_resources`,
`ad_prompts`). Module-рівневі `TRANSPORT`/`MAX_LIMIT`/`SELECTION_TTL` — тонкі
аліаси над `config` для сумісності з рештою модулів.
"""

import json
import logging
import os
from dataclasses import dataclass

from dotenv import load_dotenv

# Читаємо .env (якщо є) ще до формування Config та будь-яких конфігів.
load_dotenv()

from fastmcp import FastMCP
from fastmcp.server.auth.providers.jwt import JWTVerifier


@dataclass(frozen=True)
class Config:
    """Import-time налаштування з env (єдина точка читання)."""

    transport: str
    max_limit: int
    selection_ttl: int
    log_level: str
    log_format: str
    jwt_jwks_uri: str | None
    jwt_public_key: str | None
    jwt_issuer: str | None
    jwt_audience: str | None

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            transport=os.getenv("ADD_TRANSPORT", "stdio"),
            max_limit=int(os.getenv("ADD_MAX_LIMIT", "200")),
            selection_ttl=int(os.getenv("ADD_SELECTION_TTL", "3600")),
            log_level=os.getenv("ADD_LOG_LEVEL", "INFO"),
            log_format=os.getenv("ADD_LOG_FORMAT", "text"),
            jwt_jwks_uri=os.getenv("ADD_JWT_JWKS_URI"),
            jwt_public_key=os.getenv("ADD_JWT_PUBLIC_KEY"),
            jwt_issuer=os.getenv("ADD_JWT_ISSUER"),
            jwt_audience=os.getenv("ADD_JWT_AUDIENCE"),
        )


config = Config.from_env()

# ── Константи (не з env) ─────────────────────────────────────────────────
ALLOWED_STATUSES = ("draft", "published", "archived")

# Безпека: рівні ролей (RBAC).
# viewer  — лише читання; editor — + звичайні write; admin — + compliance/масові.
ROLES = {"viewer": 0, "editor": 1, "admin": 2}

# Поля products, дозволені в update_product_field (білий список — захист від
# SQL-ін'єкції через імʼя колонки). slug/sku/status/price/stock мають окремі тули.
ALLOWED_UPDATE_FIELDS = (
    "subtitle", "short_description", "long_description", "key_advantage",
    "package_contents", "sku_descriptor", "documentation_url",
    "highlights_intro", "specs_intro", "built_for_intro", "compatible_intro",
)

SELECTION_KEY = "selected_products"  # ключ «кошика» в стані сесії

# ── Backward-compat аліаси над config (використовуються в інших модулях) ──
TRANSPORT = config.transport
MAX_LIMIT = config.max_limit
SELECTION_TTL = config.selection_ttl


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
# ADD_LOG_LEVEL керує детальністю; ADD_LOG_FORMAT=json|text — формат.
_log_handler = logging.StreamHandler()
if config.log_format.lower() == "json":
    _log_handler.setFormatter(_JsonLogFormatter())
else:
    _log_handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s aerodefences %(message)s")
    )
logging.basicConfig(level=config.log_level, handlers=[_log_handler])
log = logging.getLogger("aerodefences")


def _build_auth():
    """JWT-автентифікація для мережевого (HTTP) транспорту.

    stdio (локальний довірений хост) authn не потребує → None.
    Для http вимагаємо джерело ключів (JWKS-URL або публічний ключ): без нього
    сервер НЕ стартує, щоб не підняти незахищений мережевий ендпоінт (fail-safe).
    """
    if config.transport != "http":
        return None
    if not (config.jwt_jwks_uri or config.jwt_public_key):
        raise RuntimeError(
            "HTTP-транспорт вимагає JWT-authn: задай ADD_JWT_JWKS_URI або ADD_JWT_PUBLIC_KEY."
        )
    return JWTVerifier(
        jwks_uri=config.jwt_jwks_uri,
        public_key=config.jwt_public_key,
        issuer=config.jwt_issuer,
        audience=config.jwt_audience,
    )


mcp = FastMCP(name="AeroDefences Catalog Server", auth=_build_auth())
