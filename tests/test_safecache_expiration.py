# Copyright 2020 Verizon Inc.
# Licensed under the terms of the Apache License 2.0.
# See LICENSE file in project root for terms.

import time

from safecache import safecache


def test_bug_expiration_ttl():
    """Bug expiration equality check was reversed and immutable
       object (cache hit) was tried to be modified.

    Reference:  https://github.com/Verizon/safecache/pull/3
    """
    @safecache(ttl=3)  # seconds
    def f(x):
        return [x]

    f(10)
    assert f.cache_info().misses == 1

    f(10)
    assert f.cache_info().misses == 1

    time.sleep(3)
    f(10)  # should be expired
    assert f.cache_info().misses == 2


def test_miss_callback_fires_on_expired_refresh():
    """miss_callback must run on every miss, including a TTL-expiry
       refresh -- not just the very first call for a given key.

    Reference: https://github.com/Verizon/safecache/pull/10
    """
    calls = []

    @safecache(ttl=0.05, miss_callback=lambda value: calls.append(value) or value)
    def f(x):
        return [x]

    f(10)
    assert calls == [[10]]

    time.sleep(0.08)

    f(10)  # expired refresh: miss_callback must be invoked again
    assert calls == [[10], [10]]