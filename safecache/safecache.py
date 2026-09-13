# Copyright 2020 Verizon Inc.
# Licensed under the terms of the Apache License 2.0.
# See LICENSE file in project root for terms.

"""
safecache.safecache
===================

This module implements safecache.
"""

from collections import deque
from collections import namedtuple
from copy import deepcopy
from functools import wraps
from hashlib import sha1
import asyncio
import inspect
import os
from threading import Event
from threading import RLock
import tempfile
from typing import Any
from typing import Callable
from typing import Dict
from typing import Text
from typing import Tuple
from typing import Type

import builtins
import math
import time
import types
import weakref

try:
    import cPickle as pickle
except ImportError:
    import pickle

IMMUTABLE_TYPES: Tuple[Type] = (
    builtins.bool,
    builtins.bytes,
    builtins.complex,
    builtins.float,
    builtins.frozenset,
    builtins.int,
    builtins.range,
    builtins.slice,
    builtins.str,
    builtins.tuple,
    builtins.type(Ellipsis),
    builtins.type(None),
    builtins.type(NotImplemented),
    builtins.type,
    types.BuiltinFunctionType,
    types.FunctionType,
    weakref.ref,
)


def is_immutable(obj: Any) -> bool:
    if not builtins.isinstance(obj, IMMUTABLE_TYPES):
        return False
    if builtins.isinstance(obj, (builtins.tuple, builtins.frozenset)):
        return all(is_immutable(item) for item in obj)
    return True


is_mutable: Callable = lambda obj: not is_immutable(obj)


def mutabletypeguard(function) -> Any:
    @wraps(function)
    def wrapper(*a, **kw) -> Any:
        result: Any = function(*a, **kw)
        if is_mutable(obj=result):
            # escalate a copy of the mutable objects to protect
            # them from mutation (by indirect operations).
            return deepcopy(result)
        return result
    return wrapper


#
# Cache information
#


class CacheDescriptor(object):
    """safecache description"""

    __slots__ = "hits", "misses", "maxsize", "currsize"

    def __init__(self, hits: int, misses: int, currsize: int, maxsize: int, **kw):
        self.currsize = currsize
        self.hits = hits
        self.maxsize = maxsize
        self.misses = misses

    def __repr__(self):
        # preserve similar semantics as lru_cache's info.
        return f"{self.__class__.__name__}(%s)" % (
            ", ".join((
                f"{slot}={repr(getattr(self, slot))}"
                for slot in iter(self.__slots__)
            ))
        )


class CacheInfo(CacheDescriptor):
    """`CacheDescriptor` alias for @lru_cache compatibility"""

    def __init__(self, *a, **kw):
        super(CacheInfo, self).__init__(*a, **kw)

    def __repr__(self):
        return super().__repr__()


#
# Cache timing (TTL)
#


now = time.time


#
# safecache
#


Cache = namedtuple("Cache", ("expiry", "value"))


def safecache(
        maxsize: int = None,
        ttl: float = math.inf,
        miss_callback: Callable = lambda _: _,
        disk_path: Text = None,
        *a, **kw):
    """safecache decorator implementation.

    Args
    ====
    maxsize -- maximum cache entry size.
    ttl -- maximum freshness of cache entry (in seconds).
    miss_callback -- custom cache-miss callback function.
    disk_path -- optional file path used to persist cache entries.
    """
    if maxsize is None:
        # static upper bound for None
        maxsize = math.inf

    elif maxsize <= 0:
        # negative is normalized to 1
        maxsize = 1

    if ttl <= .0:
        # negative time-to-live (ttl) is normalized to "always-revalidate" state.
        # This results in zero caching and 100% fetching from origin function.
        ttl = .0

    cache_mutex = RLock()

    cache: Dict = {}   # cache buffer
    hits = misses = 0  # cache stats
    in_flight: Dict = {}

    pq: deque = (  # LRU priority queue
        deque() if maxsize == math.inf
        else deque(maxlen=maxsize)
    )

    def _load_disk_cache() -> None:
        if disk_path is None or not os.path.exists(disk_path):
            return
        try:
            with open(disk_path, "rb") as cache_file:
                state = pickle.load(cache_file)
            stored_cache = state["cache"]
            stored_pq = state["pq"]
            if not isinstance(stored_cache, dict):
                return
            cache.update(stored_cache)
            pq.extend(key for key in stored_pq if key in cache)
            if maxsize != math.inf:
                while len(pq) > maxsize:
                    del cache[pq.pop()]
        except (KeyError, OSError, IOError, TypeError, ValueError, pickle.PickleError):
            cache.clear()
            pq.clear()

    def _save_disk_cache() -> None:
        if disk_path is None:
            return
        directory = os.path.dirname(os.path.abspath(disk_path))
        if not os.path.isdir(directory):
            os.makedirs(directory)
        descriptor, temporary_path = tempfile.mkstemp(
            prefix=".safecache-", dir=directory)
        try:
            with os.fdopen(descriptor, "wb") as cache_file:
                pickle.dump({"cache": cache, "pq": list(pq)}, cache_file,
                            protocol=3)
            os.replace(temporary_path, disk_path)
        except Exception:
            try:
                os.unlink(temporary_path)
            except OSError:
                pass
            raise

    _load_disk_cache()

    def _pq_inpl_swap(i: int, j: int) -> None:
        pq[i], pq[j] = pq[j], pq[i]

    def _cache_info() -> CacheInfo:
        with cache_mutex:
            return CacheInfo(
                hits=hits,
                misses=misses,
                currsize=len(cache),
                maxsize=maxsize,
            )

    def impl(function):
        if inspect.iscoroutinefunction(function):
            @wraps(function)
            async def async_wrapper(*entry, **kw):
                nonlocal hits, misses
                key: Text = sha1(pickle.dumps((*entry, kw), protocol=3)).hexdigest()
                while True:
                    with cache_mutex:
                        node = cache.get(key)
                        if (node is not None and
                                (ttl == math.inf or node.expiry > now())):
                            _pq_inpl_swap(pq.index(key), -1)
                            pq.appendleft(pq.pop())
                            hits += 1
                            result = node.value
                            break

                        state = in_flight.get(key)
                        if state is None:
                            state = {
                                "event": asyncio.Event(),
                                "error": None,
                            }
                            in_flight[key] = state
                            break

                    await state["event"].wait()
                    if state["error"] is not None:
                        raise state["error"]
                    continue

                if node is not None and (ttl == math.inf or node.expiry > now()):
                    return deepcopy(result) if is_mutable(result) else result

                try:
                    result = await function(*entry, **kw)
                    result = miss_callback(result)
                    if inspect.isawaitable(result):
                        result = await result
                    node = Cache(value=result, expiry=now() + ttl)
                    with cache_mutex:
                        if key not in cache and len(pq) == maxsize:
                            cache.__delitem__(pq.pop())
                        cache[key] = node
                        if key in pq:
                            _pq_inpl_swap(pq.index(key), -1)
                            pq.appendleft(pq.pop())
                        else:
                            pq.appendleft(key)
                        _save_disk_cache()
                        misses += 1
                    return deepcopy(result) if is_mutable(result) else result
                except BaseException as error:
                    state["error"] = error
                    raise
                finally:
                    with cache_mutex:
                        in_flight.pop(key, None)
                        state["event"].set()

            async_wrapper.cache_info = _cache_info
            return async_wrapper

        @wraps(function)
        @mutabletypeguard
        def wrapper(*entry, **kw):
            nonlocal hits, misses
            # normalize parameters to hashable strings.
            key: Text = sha1(pickle.dumps((*entry, kw), protocol=3)).hexdigest()
            while True:
                with cache_mutex:
                    node = cache.get(key)
                    if (node is not None and
                            (ttl == math.inf or node.expiry > now())):
                        _pq_inpl_swap(pq.index(key), -1)
                        pq.appendleft(pq.pop())
                        hits += 1
                        return node.value

                    event = in_flight.get(key)
                    if event is None:
                        event = in_flight[key] = Event()
                        break

                event.wait()

            try:
                if node is None:
                    result: Any = miss_callback(function(*entry, **kw))
                else:
                    result = function(*entry, **kw)
                node = Cache(value=result, expiry=now() + ttl)
                with cache_mutex:
                    if key not in cache and len(pq) == maxsize:
                        cache.__delitem__(pq.pop())
                    cache[key] = node
                    if key in pq:
                        _pq_inpl_swap(pq.index(key), -1)
                        pq.appendleft(pq.pop())
                    else:
                        pq.appendleft(key)
                    _save_disk_cache()
                    misses += 1
                return result
            finally:
                with cache_mutex:
                    event = in_flight.pop(key, None)
                    if event is not None:
                        event.set()
        wrapper.cache_info = _cache_info
        return wrapper
    return impl
