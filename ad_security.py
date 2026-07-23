"""
Безпека: визначення ролі (RBAC), бар'єр контролю доступу та підтвердження.

Роль виводиться на СЕРВЕРІ (з перевіреного JWT у HTTP або з env у stdio) —
клієнтські `meta` на неї НЕ впливають. Кожен write проходить `_confirm`:
спершу RBAC, потім явне підтвердження людини (elicitation).
"""

import os

from fastmcp.server.context import Context
from fastmcp.server.dependencies import get_access_token
from fastmcp.server.elicitation import (
    AcceptedElicitation,
    CancelledElicitation,
    DeclinedElicitation,
)

from ad_config import ROLES, TRANSPORT, log
from ad_metrics import metrics


def _role_from_token() -> str | None:
    """Роль із ПЕРЕВІРЕНОГО JWT: спершу claim `role`, потім scopes.
    None — якщо токена немає або роль не розпізнано."""
    try:
        token = get_access_token()
    except Exception:
        return None
    if token is None:
        return None
    claims = getattr(token, "claims", None) or {}
    role = claims.get("role")
    if role in ROLES:
        return role
    scopes = set(getattr(token, "scopes", None) or [])
    for r in ("admin", "editor", "viewer"):
        if r in scopes or f"role:{r}" in scopes:
            return r
    return None


def _current_role(ctx: Context) -> str:
    """Роль поточного виклику. Джерело залежить від транспорту:

    • http  — ЛИШЕ перевірений JWT (claim `role` / scopes). Клієнтські `meta`
      більше НЕ впливають на роль (усунення обходу RBAC). Автентифікований,
      але без валідної ролі → мінімальна `viewer`.
    • stdio — локальний довірений хост: роль з env. Дефолт `viewer` (fail-safe);
      dev може підняти через `ADD_DEV_ROLE`.
    """
    if TRANSPORT == "http":
        return _role_from_token() or "viewer"
    role = os.getenv("ADD_DEV_ROLE") or os.getenv("ADD_ROLE", "viewer")
    return role if role in ROLES else "viewer"


def _require_role(ctx: Context, minimum: str) -> None:
    """Кидає PermissionError, якщо роль виклику нижча за потрібну."""
    role = _current_role(ctx)
    if ROLES[role] < ROLES[minimum]:
        metrics.record_denied()
        log.warning("access denied: role=%s needs=%s", role, minimum)
        raise PermissionError(
            f"Недостатньо прав: потрібна роль '{minimum}', поточна '{role}'."
        )


async def _confirm(ctx: Context, message: str, min_role: str = "editor") -> tuple[bool, str]:
    """Спільний бар'єр для write-операцій: спершу RBAC, потім підтвердження людини.
    Повертає (ok, reason): ok=True якщо роль достатня І користувач відповів 'yes',
    інакше ok=False і текст причини скасування для відповіді клієнту."""
    _require_role(ctx, min_role)  # бар'єр контролю доступу ДО діалогу підтвердження
    confirm = await ctx.elicit(message, response_type=["yes", "no"])
    match confirm:
        case AcceptedElicitation(data="yes"):
            return True, ""
        case AcceptedElicitation(data="no"):
            await ctx.info("Користувач відповів 'no' — зміну скасовано")
            return False, "Скасовано користувачем (відповідь 'no')."
        case DeclinedElicitation():
            await ctx.info("Користувач відхилив підтвердження — зміну скасовано")
            return False, "Скасовано: підтвердження не надано."
        case CancelledElicitation():
            await ctx.info("Користувач перервав операцію")
            return False, "Операцію перервано."
    return False, "Скасовано."
