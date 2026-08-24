"""Le fallback mémoire doit offrir exactement les mêmes garanties que Redis."""

from __future__ import annotations

import time


async def test_strings_and_ttl(store):
    await store.set("k", "v", ttl=1)
    assert await store.get("k") == "v"
    time.sleep(1.1)
    assert await store.get("k") is None


async def test_json_roundtrip(store):
    await store.set_json("hm:hb:x", {"service": "x", "status": "ok"})
    assert (await store.get_json("hm:hb:x"))["status"] == "ok"


async def test_sets(store):
    await store.sadd("s", "a", "b")
    assert await store.sismember("s", "a")
    assert not await store.sismember("s", "c")
    assert await store.smembers("s") == {"a", "b"}


async def test_lists_and_trim(store):
    for i in range(5):
        await store.rpush("l", str(i))
    assert await store.llen("l") == 5
    await store.ltrim("l", -3, -1)
    assert await store.lrange("l", 0, -1) == ["2", "3", "4"]
    assert await store.lpop("l") == "2"


async def test_claim_is_single_shot(store):
    assert await store.claim("once", 60) is True
    assert await store.claim("once", 60) is False


async def test_rate_limit_window(store):
    assert [await store.incr_window("ip", 60) for _ in range(3)] == [1, 2, 3]


async def test_publish_without_redis_reports_failure(store):
    assert await store.publish("chan", "{}") is False
