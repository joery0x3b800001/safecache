# Copyright 2020 Verizon Inc.
# Licensed under the terms of the Apache License 2.0.
# See LICENSE file in project root for terms.

"""
tests.safecache_cacheinfo
=========================
"""

import threading
import time
import asyncio

from safecache import safecache


def test_async_cache_awaits_and_reuses_result():
	calls = []

	@safecache()
	async def function(value):
		calls.append(value)
		return {"value": value}

	async def run():
		first = await function(1)
		second = await function(1)
		first["value"] = 2
		return second

	assert asyncio.run(run()) == {"value": 1}
	assert calls == [1]
	assert function.cache_info().hits == 1
	assert function.cache_info().misses == 1


def test_async_concurrent_miss_only_calls_function_once():
	calls = []

	async def run():
		started = asyncio.Event()
		release = asyncio.Event()

		@safecache()
		async def function(value):
			calls.append(value)
			started.set()
			await release.wait()
			return value

		tasks = [asyncio.create_task(function(1)) for _ in range(5)]
		await started.wait()
		release.set()
		result = await asyncio.gather(*tasks)
		return result, function

	result, function = asyncio.run(run())
	assert result == [1] * 5
	assert calls == [1]
	assert function.cache_info().misses == 1
	assert function.cache_info().hits == 4


def test_async_ttl_expires_result():
	calls = []

	@safecache(ttl=0.05)
	async def function():
		calls.append(None)
		return len(calls)

	async def run():
		first = await function()
		await asyncio.sleep(0.08)
		second = await function()
		return first, second

	assert asyncio.run(run()) == (1, 2)
	assert len(calls) == 2


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
