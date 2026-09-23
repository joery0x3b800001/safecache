# Copyright 2020 Verizon Inc.
# Licensed under the terms of the Apache License 2.0.
# See LICENSE file in project root for terms.

import asyncio
import time

import pytest

from safecache import safecache
from safecache.exceptions import CacheError
from safecache.exceptions import CacheExpired
from safecache.exceptions import CacheMiss


def test_cache_get_raises_cache_miss_for_an_unpopulated_key():
    @safecache()
    def f(x):
        return x

    with pytest.raises(CacheMiss):
        f.cache_get(10)

    # cache_get must not have invoked the wrapped function.
    assert f.cache_info().misses == 0


def test_cache_get_returns_a_cached_value_and_counts_as_a_hit():
    calls = []

    @safecache()
    def f(x):
        calls.append(x)
        return [x]

    f(10)
    assert f.cache_get(10) == [10]
    assert calls == [10]

    info = f.cache_info()
    assert info.hits == 1
    assert info.misses == 1


def test_cache_get_returns_an_isolated_copy_of_mutable_values():
    @safecache()
    def f(x):
        return [x]

    f(10)
    value = f.cache_get(10)
    value.append("mutated")

    assert f.cache_get(10) == [10]


def test_cache_get_raises_cache_expired_after_ttl_elapses():
    @safecache(ttl=0.05)
    def f(x):
        return x

    f(10)
    assert f.cache_get(10) == 10

    time.sleep(0.08)

    with pytest.raises(CacheExpired):
        f.cache_get(10)


def test_cache_miss_and_cache_expired_are_catchable_as_cache_error():
    assert issubclass(CacheMiss, CacheError)
    assert issubclass(CacheMiss, KeyError)
    assert issubclass(CacheExpired, CacheError)
    assert issubclass(CacheExpired, ValueError)


def test_async_cache_get_raises_cache_miss_and_reads_cached_value():
    @safecache()
    async def f(x):
        return {"value": x}

    async def run():
        with pytest.raises(CacheMiss):
            f.cache_get(1)

        await f(1)
        return f.cache_get(1)

    assert asyncio.run(run()) == {"value": 1}