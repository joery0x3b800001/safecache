# Copyright 2020 Verizon Inc.
# Licensed under the terms of the Apache License 2.0.
# See LICENSE file in project root for terms.

"""
tests.safecache_cacheinfo
=========================
"""

import threading
import time

from safecache import safecache


def test_cache_info_reports_hits_misses_and_size():
	@safecache(maxsize=2)
	def function(value):
		return value

	assert function.cache_info().currsize == 0
	function(1)
	function(1)

	info = function.cache_info()
	assert info.hits == 1
	assert info.misses == 1
	assert info.currsize == 1
	assert info.maxsize == 2


def test_lru_order_is_updated_on_hits():
	calls = []

	@safecache(maxsize=2)
	def function(value):
		calls.append(value)
		return value

	function("a")
	function("b")
	function("a")
	function("c")
	function("b")

	assert calls == ["a", "b", "c", "b"]


def test_concurrent_miss_only_calls_function_once():
	calls = []
	started = threading.Event()
	release = threading.Event()

	@safecache()
	def function(value):
		calls.append(value)
		started.set()
		release.wait(timeout=2)
		return value

	threads = [threading.Thread(target=function, args=(1,)) for _ in range(5)]
	for thread in threads:
		thread.start()
	assert started.wait(timeout=2)
	release.set()
	for thread in threads:
		thread.join(timeout=2)

	assert calls == [1]
	assert function.cache_info().misses == 1
	assert function.cache_info().hits == 4


def test_fractional_ttl_is_respected():
	calls = []

	@safecache(ttl=0.05)
	def function():
		calls.append(None)
		return len(calls)

	assert function() == 1
	assert function() == 1
	time.sleep(0.08)
	assert function() == 2
	assert calls == [None, None]
