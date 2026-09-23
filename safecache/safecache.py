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
from threading import Lock
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

from .exceptions import CacheExpired
from .exceptions import CacheMiss

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

    pq: deque = deque()  # LRU priority queue
    disk_io_lock = Lock()
    _log_path: Text = (disk_path + ".log") if disk_path is not None else None
    _log_record_count = 0

    def _compact_disk_cache_locked(snapshot_cache: Dict, snapshot_pq: list) -> None:
        """Rewrite disk_path as a single compact snapshot and clear the
        log. Caller must hold disk_io_lock. Blocking -- keep off the
        event loop."""
        nonlocal _log_record_count
        directory = os.path.dirname(os.path.abspath(disk_path))
        if not os.path.isdir(directory):
            os.makedirs(directory)
        descriptor, temporary_path = tempfile.mkstemp(
            prefix=".safecache-", dir=directory)
        try:
            with os.fdopen(descriptor, "wb") as cache_file:
                pickle.dump({"cache": snapshot_cache, "pq": snapshot_pq},
                            cache_file, protocol=3)
            os.replace(temporary_path, disk_path)
        except Exception:
            try:
                os.unlink(temporary_path)
            except OSError:
                pass
            raise
        try:
            open(_log_path, "wb").close()
        except OSError:
            pass
        _log_record_count = 0

    def _append_disk_log(entry: Tuple, currsize: int) -> None:
        """Append one incremental change -- ("set", key, node) or
        ("evict", key) -- to the on-disk log. Blocking; callers must
        already have released cache_mutex before calling this (and, for
        the async wrapper, run it via an executor) so disk latency never
        blocks other cache lookups."""
        if disk_path is None:
            return
        nonlocal _log_record_count
        with disk_io_lock:
            directory = os.path.dirname(os.path.abspath(disk_path))
            if not os.path.isdir(directory):
                os.makedirs(directory)
            try:
                with open(_log_path, "ab") as log_file:
                    pickle.dump(entry, log_file, protocol=3)
            except (OSError, IOError, pickle.PickleError):
                return
            _log_record_count += 1
            # Amortize: once the log has grown past a small multiple of
            # the cache's own size, fold it into a fresh compact
            # snapshot rather than letting it grow without bound.
            if _log_record_count >= max(32, 4 * max(currsize, 1)):
                with cache_mutex:
                    snapshot_cache = dict(cache)
                    snapshot_pq = list(pq)
                try:
                    _compact_disk_cache_locked(snapshot_cache, snapshot_pq)
                except Exception:
                    pass

    def _load_disk_cache() -> None:
        if disk_path is None:
            return

        loaded_any = False

        if os.path.exists(disk_path):
            try:
                with open(disk_path, "rb") as cache_file:
                    state = pickle.load(cache_file)
                stored_cache = state["cache"]
                stored_pq = state["pq"]
                if isinstance(stored_cache, dict):
                    cache.update(stored_cache)
                    pq.extend(key for key in stored_pq if key in cache)
                    loaded_any = True
            except (KeyError, OSError, IOError, TypeError, ValueError, pickle.PickleError):
                cache.clear()
                pq.clear()

        if os.path.exists(_log_path):
            try:
                with open(_log_path, "rb") as log_file:
                    while True:
                        try:
                            entry = pickle.load(log_file)
                        except EOFError:
                            break
                        if entry[0] == "set":
                            _, key, node = entry
                            cache[key] = node
                            try:
                                pq.remove(key)
                            except ValueError:
                                pass
                            pq.appendleft(key)
                        elif entry[0] == "evict":
                            _, key = entry
                            cache.pop(key, None)
                            try:
                                pq.remove(key)
                            except ValueError:
                                pass
                loaded_any = True
            except (OSError, IOError, TypeError, ValueError, pickle.PickleError):
                pass

        if maxsize != math.inf:
            while len(pq) > maxsize:
                del cache[pq.pop()]

        if loaded_any:
            with disk_io_lock:
                try:
                    _compact_disk_cache_locked(dict(cache), list(pq))
                except Exception:
                    pass

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

    def _cache_get(*entry, **kw) -> Any:
        """Look up a call's cached value without invoking the wrapped
        function. Raises CacheMiss if nothing has ever been cached for
        these arguments, or CacheExpired if a cached entry exists but its
        TTL has elapsed. A successful lookup counts as a hit and moves the
        entry to the front of the LRU queue, exactly like a normal call.
        """
        nonlocal hits
        key: Text = sha1(pickle.dumps((*entry, kw), protocol=3)).hexdigest()
        with cache_mutex:
            node = cache.get(key)
            if node is None:
                raise CacheMiss(key)
            if ttl != math.inf and node.expiry <= now():
                raise CacheExpired(key)
            try:
                pq.remove(key)
            except ValueError:
                pass
            pq.appendleft(key)
            hits += 1
            value = node.value
        return deepcopy(value) if is_mutable(value) else value

    def impl(function):
        if inspect.iscoroutinefunction(function):
            @wraps(function)
            async def async_wrapper(*entry, **kw):

                nonlocal hits, misses

                key: Text = sha1(
                    pickle.dumps((*entry, kw), protocol=3)
                ).hexdigest()

                with cache_mutex:
                    node = cache.get(key)

                    if (
                        node is not None
                        and (
                            ttl == math.inf
                            or node.expiry > now()
                        )
                    ):
                        try:
                            pq.remove(key)
                        except ValueError:
                            pass

                        pq.appendleft(key)

                        hits += 1

                        result = node.value

                        return (
                            deepcopy(result)
                            if is_mutable(result)
                            else result
                        )

                    future = in_flight.get(key)

                    if future is None:
                        loop = asyncio.get_running_loop()
                        future = loop.create_future()
                        in_flight[key] = future
                        owner = True
                    else:
                        owner = False

                if not owner:
                    try:
                        result = await asyncio.shield(future)
                    except asyncio.CancelledError:
                        raise
                    except BaseException:
                        hits += 1
                        raise

                    hits += 1

                    return (
                        deepcopy(result)
                        if is_mutable(result)
                        else result
                    )

                try:
                    result = await function(*entry, **kw)

                    # miss_callback may be synchronous or asynchronous.
                    result = miss_callback(result)

                    if inspect.isawaitable(result):
                        result = await result

                    node = Cache(
                        value=result,
                        expiry=now() + ttl,
                    )

                    evicted_key = None

                    with cache_mutex:

                        if (
                            key not in cache
                            and maxsize != math.inf
                            and len(pq) >= maxsize
                        ):
                            evicted_key = pq.pop()
                            cache.pop(evicted_key, None)

                        cache[key] = node

                        try:
                            pq.remove(key)
                        except ValueError:
                            pass

                        pq.appendleft(key)

                        misses += 1

                        currsize = len(cache)

                    if disk_path is not None:
                        loop = asyncio.get_running_loop()
                        if evicted_key is not None:
                            await loop.run_in_executor(
                                None, _append_disk_log,
                                ("evict", evicted_key), currsize)
                        await loop.run_in_executor(
                            None, _append_disk_log,
                            ("set", key, node), currsize)

                    if not future.done():
                        future.set_result(result)

                    return (
                        deepcopy(result)
                        if is_mutable(result)
                        else result
                    )

                except BaseException as error:
                    if not future.done():
                        future.set_exception(error)

                        # Avoid "Future exception was never retrieved"
                        # if there are no waiters.
                        future.add_done_callback(
                            lambda f: f.exception()
                        )

                    raise

                finally:
                    with cache_mutex:
                        if in_flight.get(key) is future:
                            in_flight.pop(key, None)

            async_wrapper.cache_info = _cache_info
            async_wrapper.cache_get = _cache_get
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
                result: Any = miss_callback(function(*entry, **kw))
                node = Cache(value=result, expiry=now() + ttl)
                evicted_key = None
                with cache_mutex:
                    if key not in cache and len(pq) == maxsize:
                        evicted_key = pq.pop()
                        cache.__delitem__(evicted_key)
                    cache[key] = node
                    if key in pq:
                        _pq_inpl_swap(pq.index(key), -1)
                        pq.appendleft(pq.pop())
                    else:
                        pq.appendleft(key)
                    misses += 1
                    currsize = len(cache)

                if disk_path is not None:
                    if evicted_key is not None:
                        _append_disk_log(("evict", evicted_key), currsize)
                    _append_disk_log(("set", key, node), currsize)

                return result
            finally:
                with cache_mutex:
                    event = in_flight.pop(key, None)
                    if event is not None:
                        event.set()
        wrapper.cache_info = _cache_info
        wrapper.cache_get = _cache_get
        return wrapper
    return impl