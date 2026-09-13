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