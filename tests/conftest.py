from __future__ import annotations

import os

import pytest

# Fixé avant tout import de l'app : `get_settings` est mis en cache.
os.environ.setdefault("HM_INGEST_TOKEN", "test-token")
os.environ.setdefault("REDIS_URL", "")
os.environ.setdefault("HM_SERVICES", "moddy-bot,moddy-api")
os.environ.setdefault("HM_CRITICAL_SERVICES", "moddy-bot,moddy-api")
os.environ.setdefault("HM_STARTUP_GRACE", "0")
os.environ.setdefault("DISCORD_WEBHOOK_URL", "")
os.environ.setdefault("BETTERSTACK_TOKEN", "")
os.environ.setdefault("BETTERSTACK_INDEX_URL", "")
os.environ.setdefault("HM_SELF_HEARTBEAT_URL", "")


@pytest.fixture
def store():
    from app.state import Store

    return Store("")


@pytest.fixture
def settings(**_):
    from app.config import Settings

    return Settings(
        hm_services="moddy-bot,moddy-api",
        hm_critical_services="moddy-bot,moddy-api",
        hm_failure_threshold=3,
        hm_recovery_threshold=2,
        hm_startup_grace=0,
        hm_min_silence=0,
        redis_url="",
    )
