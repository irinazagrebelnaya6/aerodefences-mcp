"""
API Gateway для embeddings (мікросервісний патерн з лекції, застосований до
embeddings-підсистеми).

Єдиний вхід `.embed()`, який маршрутизує запити до кількох провайдерів за
політикою з упорядкованим фолбеком — аналог gateway, що обирає між моделями
(GPT/Claude/Llama), лише для embeddings-бекендів.

Провайдери «з коробки»:
  - основний — Voyage (прямий або через sidecar, з `ad_embeddings`);
  - резервний — `LocalEmbedProvider`: офлайн детермінований (hashing bag-of-token),
    без мережі/ключа. Якість нижча, але це СПРАВЖНІЙ другий провайдер — дає
    роутингу реальний вибір і graceful-фолбек Voyage→local (замість падіння в
    TF-IDF), плюс офлайн-режим.

⚠️ Вектори різних провайдерів НЕ в одному просторі. Тому gateway **фіксується**
на провайдері, який успішно обслужив ПЕРШИЙ виклик (build корпусу), і далі
використовує лише його — щоб вектори документів і запиту були порівнянні.
Додати нового провайдера (OpenAI, локальна модель) — зареєструвати у `build_gateway`.
"""

from __future__ import annotations

import hashlib
import math

from ad_config import config, log
from voyage_client import EmbeddingsError


class LocalEmbedProvider:
    """Офлайн детермінований embed-провайдер: hashing bag-of-token → вектор."""

    model = "local-hash"

    def __init__(self, dim: int) -> None:
        self.dim = dim

    async def embed(self, texts: list[str], input_type: str) -> list[list[float]]:
        return [self._vec(t) for t in texts]

    def _vec(self, text: str) -> list[float]:
        from rag_index import _tokenize  # лінивий імпорт, щоб уникнути циклів
        v = [0.0] * self.dim
        for tok in _tokenize(text):
            h = int(hashlib.sha1(tok.encode("utf-8")).hexdigest(), 16)
            v[h % self.dim] += 1.0
        norm = math.sqrt(sum(x * x for x in v)) or 1.0
        return [x / norm for x in v]


class EmbeddingsGateway:
    """Маршрутизує `.embed()` до впорядкованих провайдерів із фолбеком.

    Блокується на першому провайдері, що успішно обслужив виклик, і далі
    використовує лише його (щоб не змішувати векторні простори)."""

    def __init__(self, providers: list[tuple[str, object]]) -> None:
        if not providers:
            raise EmbeddingsError("gateway: не налаштовано жодного провайдера")
        self.providers = providers
        self._active: tuple[str, object] | None = None
        first = providers[0][1]
        self.model = getattr(first, "model", "gateway")
        self.dim = getattr(first, "dim", config.embed_dim)

    async def embed(self, texts: list[str], input_type: str) -> list[list[float]]:
        # вже зафіксований провайдер — тільки він (без мовчазного перемикання)
        if self._active is not None:
            return await self._active[1].embed(texts, input_type)
        errors = []
        for name, client in self.providers:
            try:
                out = await client.embed(texts, input_type)
            except EmbeddingsError as e:
                errors.append(f"{name}: {e}")
                log.warning("gateway: провайдер '%s' недоступний → наступний", name)
                continue
            self._active = (name, client)
            self.model = getattr(client, "model", name)
            self.dim = getattr(client, "dim", self.dim)
            log.info("gateway: маршрутизовано на провайдера '%s'", name)
            return out
        raise EmbeddingsError("gateway: усі провайдери недоступні: " + "; ".join(errors))

    @property
    def active_provider(self) -> str | None:
        return self._active[0] if self._active else None


def build_gateway() -> EmbeddingsGateway:
    """Зібрати gateway: основний провайдер (Voyage/sidecar, якщо є) + local-фолбек."""
    from ad_embeddings import _primary_provider

    providers: list[tuple[str, object]] = []
    try:
        providers.append(_primary_provider())
    except EmbeddingsError:
        log.info("gateway: хмарний провайдер не заданий — лише local")
    providers.append(("local", LocalEmbedProvider(config.embed_dim)))
    return EmbeddingsGateway(providers)
