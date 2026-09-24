# Copyright 2020 Verizon Inc.
# Licensed under the terms of the Apache License 2.0.
# See LICENSE file in project root for terms.

from safecache import safecache


def test_disk_cache_is_reused_by_a_new_decorator(tmp_path):
    cache_path = str(tmp_path / "cache.pkl")
    calls = []

    @safecache(disk_path=cache_path)
    def first(value):
        calls.append(value)
        return {"value": value}

    assert first(10) == {"value": 10}
    assert calls == [10]

    @safecache(disk_path=cache_path)
    def second(value):
        calls.append(value)
        return {"value": value}

    assert second(10) == {"value": 10}
    assert calls == [10]


def test_disk_cache_recovers_from_invalid_file(tmp_path):
    cache_path = tmp_path / "cache.pkl"
    cache_path.write_text("not a pickle")
    calls = []

    @safecache(disk_path=str(cache_path))
    def function(value):
        calls.append(value)
        return value

    assert function(10) == 10
    assert calls == [10]


def test_disk_cache_respects_maxsize(tmp_path):
    cache_path = str(tmp_path / "cache.pkl")
    calls = []

    @safecache(maxsize=1, disk_path=cache_path)
    def function(value):
        calls.append(value)
        return value

    function(1)
    function(2)

    @safecache(maxsize=1, disk_path=cache_path)
    def restored(value):
        calls.append(value)
        return value

    restored(1)
    assert calls == [1, 2, 1]


def test_disk_maxsize_shrink_across_restart_does_not_crash(tmp_path):
    """Reopening a disk-persisted cache under a *smaller* maxsize than it
    was written with must not desync the cache dict from the LRU queue.

    Previously `pq` was built as `deque(maxlen=maxsize)`, and
    `deque.extend()` silently drops overflow from the wrong end when a
    restored queue is longer than the new maxsize. That left stale keys
    in `cache` but not in `pq`, and touching one of them crashed with
    ValueError("... is not in deque") on the very next hit.
    """
    cache_path = str(tmp_path / "cache.pkl")

    @safecache(maxsize=10, disk_path=cache_path)
    def f(x):
        return x

    for i in range(10):
        f(i)

    @safecache(maxsize=3, disk_path=cache_path)
    def g(x):
        return x

    assert g.cache_info().currsize <= 3

    for i in range(10):
        g(i)  # must not raise


def test_disk_cache_write_is_incremental_not_a_full_rewrite(tmp_path, monkeypatch):
    """Every miss must cost O(1) disk work, not O(currsize). Previously
    the whole cache dict was re-pickled and rewritten to disk_path on
    every single miss, so filling an n-entry cache cost O(n^2) total I/O.
    Assert full-file rewrites (os.replace) stay rare -- amortized, not
    once per miss -- as the cache grows.
    """
    import os

    cache_path = str(tmp_path / "cache.pkl")
    calls = {"n": 0}
    real_replace = os.replace

    def counting_replace(src, dst):
        calls["n"] += 1
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", counting_replace)

    @safecache(disk_path=cache_path)
    def f(x):
        return f"{x}" + ("y" * 500)  # distinct, non-trivial payload per key

    misses = 500
    for i in range(misses):
        f(i)

    assert calls["n"] < misses // 4


def test_disk_cache_log_stays_bounded_for_a_stable_key_set(tmp_path):
    """A small, stable set of keys refreshed repeatedly (e.g. TTL churn)
    must have its incremental log folded back into a compact snapshot
    periodically, rather than growing without bound.
    """
    import os
    import time

    cache_path = str(tmp_path / "cache.pkl")

    @safecache(ttl=0.01, disk_path=cache_path)
    def f(x):
        return x

    for round_ in range(400):
        f(round_ % 5)  # only 5 distinct keys, refreshed over and over
        time.sleep(0.001)

    log_path = cache_path + ".log"
    log_size = os.path.getsize(log_path) if os.path.exists(log_path) else 0
    assert log_size < 8000


def test_disk_compact_after_caps_log_growth_independent_of_maxsize(tmp_path):
    """The adaptive compaction threshold (max(32, 4*currsize)) scales
    with the cache's own size, so a large maxsize with only a small hot
    subset under a short ttl can let the log grow much larger than the
    live key count would suggest before it's ever folded down.
    disk_compact_after caps that regardless of currsize.
    """
    import os
    import time

    cache_path = str(tmp_path / "cache.pkl")

    @safecache(maxsize=5000, ttl=0.005, disk_path=cache_path,
               disk_compact_after=50)
    def f(x):
        return x

    for i in range(1000):
        f(("filler", i))  # push currsize up with a large, distinct set

    for round_ in range(300):
        f(round_ % 5)  # small hot subset, repeatedly refreshed
        time.sleep(0.0002)

    log_path = cache_path + ".log"
    log_size = os.path.getsize(log_path) if os.path.exists(log_path) else 0
    assert log_size < 20000


def test_disk_compact_after_negative_is_normalized(tmp_path):
    cache_path = str(tmp_path / "cache.pkl")

    @safecache(disk_path=cache_path, disk_compact_after=-5)
    def f(x):
        return x

    for i in range(5):
        f(i)

    assert f.cache_info().currsize == 5


def test_disk_cache_write_does_not_block_event_loop(tmp_path):
    """The disk write for an async-decorated function must run off the
    event loop thread, not synchronously inside the coroutine, so a slow
    disk cannot stall every other coroutine scheduled on the loop.
    """
    import asyncio
    import builtins
    import threading

    cache_path = str(tmp_path / "cache.pkl")
    loop_thread_id = {}
    write_thread_id = {}
    real_open = builtins.open

    def spying_open(path, mode="r", *a, **kw):
        if isinstance(path, str) and path.endswith(".log") and "a" in mode:
            write_thread_id["id"] = threading.get_ident()
        return real_open(path, mode, *a, **kw)

    @safecache(disk_path=cache_path)
    async def g(x):
        return x

    async def run():
        loop_thread_id["id"] = threading.get_ident()
        builtins.open = spying_open
        try:
            await g(1)
        finally:
            builtins.open = real_open

    asyncio.run(run())

    assert write_thread_id.get("id") is not None
    assert write_thread_id["id"] != loop_thread_id["id"]