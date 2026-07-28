"""Тести API Gateway для embeddings (без мережі)."""

import pytest

from ad_gateway import EmbeddingsGateway, LocalEmbedProvider
from voyage_client import EmbeddingsError


class OkProvider:
    model = "ok-model"
    dim = 4
    def __init__(self, name):
        self.name = name
        self.calls = 0
    async def embed(self, texts, input_type):
        self.calls += 1
        return [[1.0, 0.0, 0.0, 0.0] for _ in texts]


class FailProvider:
    model = "bad"
    dim = 4
    async def embed(self, texts, input_type):
        raise EmbeddingsError("down")


async def test_routes_to_primary():
    primary = OkProvider("voyage")
    gw = EmbeddingsGateway([("voyage", primary), ("local", OkProvider("local"))])
    out = await gw.embed(["hi"], "document")
    assert out and gw.active_provider == "voyage"
    assert gw.model == "ok-model"


async def test_falls_back_to_next_provider():
    local = OkProvider("local")
    gw = EmbeddingsGateway([("voyage", FailProvider()), ("local", local)])
    await gw.embed(["hi"], "document")
    assert gw.active_provider == "local"
    assert local.calls == 1


async def test_locks_to_active_provider():
    """Після фіксації всі виклики йдуть лише в активний провайдер
    (щоб вектори корпусу й запиту були в одному просторі)."""
    primary = OkProvider("voyage")
    second = OkProvider("local")
    gw = EmbeddingsGateway([("voyage", primary), ("local", second)])
    await gw.embed(["build"], "document")   # фіксує voyage
    await gw.embed(["query"], "query")
    assert primary.calls == 2 and second.calls == 0


async def test_all_providers_fail_raises():
    gw = EmbeddingsGateway([("voyage", FailProvider()), ("local", FailProvider())])
    with pytest.raises(EmbeddingsError):
        await gw.embed(["hi"], "document")


async def test_local_provider_deterministic():
    p = LocalEmbedProvider(dim=16)
    a = (await p.embed(["камера тепловізор"], "query"))[0]
    b = (await p.embed(["камера тепловізор"], "query"))[0]
    assert a == b                      # детермінований
    assert len(a) == 16
    assert abs(sum(x * x for x in a) - 1.0) < 1e-6  # нормований


async def test_build_gateway_local_only(monkeypatch):
    """Без хмарного провайдера gateway працює лише з local (офлайн)."""
    from types import SimpleNamespace

    import ad_gateway
    monkeypatch.setattr(
        "ad_embeddings.config",
        SimpleNamespace(embeddings_url=None, voyage_api_key=None,
                        embed_gateway=True, embed_model="m", embed_dim=8),
    )
    monkeypatch.setattr(ad_gateway, "config",
                        SimpleNamespace(embed_dim=8))
    gw = ad_gateway.build_gateway()
    out = await gw.embed(["привіт"], "document")
    assert gw.active_provider == "local"
    assert len(out[0]) == 8
