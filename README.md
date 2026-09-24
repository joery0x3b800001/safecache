[![PyPI](https://img.shields.io/badge/package-pypi-blue.svg)](https://pypi.org/project/safecache/)
![GitHub Actions: python-pytest](https://github.com/Verizon/safecache/workflows/python-pytest/badge.svg)

<div align="center">
  <h1>safecache</h1>
  <p><strong>A thread-safe and mutation-safe LRU cache for Python.</strong></p>
</div>

## Features

- Zero third-party dependencies.
- All cached entries are **mutation-safe**.
- All cached entries are **thread-safe**.
- Customizable cache-miss behavior.
- Optional disk caching.
- Inspect a cached value directly, without invoking the wrapped function.

## Installation

```bash
~$ pip install safecache
```

## Usage

**safecache** works just like the [functool's lru_cache](https://docs.python.org/3/library/functools.html#functools.lru_cache) where you decorate a callable with [optional configurations](#cache-configurations).

```python
from safecache import safecache

@safecache()
def fib(n):
    x, y, z = 0, 0, 1
    while n:
        n -= 1
        x, y = y, z
        z = x + y
    return y
```

Once decorated, the callable will inherit the [functionality](#features) of **safecache** and begin safely caching returned results.

Async callables are supported as well. The decorated function awaits the
origin call and caches its resolved result:

```python
from safecache import safecache

@safecache(ttl=60)
async def fetch_value(value):
    return await fetch_from_service(value)

result = await fetch_value("key")
```

## Cache Configurations

| Parameter | Description | Default |
|:----------|:------------|:--------|
| `maxsize`| maximum cache entry size. | `None` |
| `ttl`| maximum freshness of cache entry (in seconds). | `math.inf` |
| `miss_callback` | custom cache-miss callback function (e.g. [Redis](https://redis.io) client). | `lambda _: _` |
| `disk_path` | file path used to persist cache entries between processes. | `None` |
| `disk_compact_after` | cap, in records, on how large `disk_path`'s on-disk log can grow before it's folded into a snapshot. `None` uses the adaptive `max(32, 4 * currsize)` threshold. | `None` |

To persist entries between processes, pass a file path to `disk_path`:

```python
from safecache import safecache

@safecache(disk_path=".cache/fib.pkl")
def fib(n):
    if n <= 1:
        return n
    return fib(n - 1) + fib(n - 2)
```

The cache file is written after a miss or expiration refresh. Writes are
incremental: each miss appends a small record to `<disk_path>.log` rather
than re-serializing the whole cache, and that log is periodically folded
back into a compact snapshot at `disk_path` once it grows past a small
multiple of the cache's own size. This keeps persistence at roughly O(1)
work per miss instead of O(currsize) -- filling a large cache no longer
means rewriting an ever-larger file on every single call. All disk I/O runs
outside the cache's own lock (and, for `async` functions, off the event
loop entirely via the default executor), so a slow disk only delays other
writers to the same `disk_path`, never an ordinary cache lookup. Cache
statistics are kept per process, while `ttl` continues to apply to
persisted entries.

The default compaction threshold (`max(32, 4 * currsize)`) scales with the
cache's own size. That's fine for a typical cache, but if `maxsize` is
large while only a small hot subset of keys is actually being refreshed
(e.g. under a short `ttl`), the log can grow well past what the live key
count would suggest before it's ever folded down -- currsize includes
every key, not just the ones churning. Pass `disk_compact_after` to cap
the log at a fixed number of records regardless of `currsize`:

```python
@safecache(disk_path=".cache/fib.pkl", maxsize=5000, ttl=30,
           disk_compact_after=200)
def fib(n):
    ...
```

Two levers, two different jobs: `maxsize` bounds how many entries are
kept at all (evicting the least-recently-used ones); `disk_compact_after`
bounds how large the *uncompacted log* is allowed to get in between
snapshots, independent of how many entries are currently live. Note that
`ttl` on its own does not shrink anything -- an expired entry just sits in
the cache (and on disk) until it's either looked up again or evicted by
`maxsize` pressure, so unbounded, high-cardinality keys will grow both the
in-memory cache and its disk persistence without bound unless `maxsize`
is set.

Disk persistence is best-effort: a write failure is swallowed rather than
raised, so it never breaks the in-memory cache the caller is waiting on,
and a corrupt or truncated log tail is skipped rather than discarding
everything that came before it. It is not, however, safe for multiple
processes to write to the same `disk_path` *concurrently* -- there is no
cross-process file locking, so two processes persisting at the same time
can silently drop each other's entries. Treat `disk_path` as a
single-writer, best-effort warm-start cache, not a shared live store.

> **Important:** `disk_path` identifies the persisted cache itself, not the
> decorated function -- entries are keyed only by their serialized call
> arguments. Reusing the same `disk_path` for a new process running the
> *same* logical function is exactly how cross-process persistence is meant
> to work. But pointing **two different functions** at the same `disk_path`
> is unsafe: if both happen to be called with arguments that serialize the
> same way, one function's cached result can be returned for the other, and
> each save overwrites the other's persisted state. Give every distinct
> cached function its own `disk_path`.
>
> Also note that `disk_path` is trusted input: loading it uses `pickle`,
> which can execute arbitrary code for a maliciously crafted file. Only
> point `disk_path` at a file your own process controls.

## Cache Statistics

To view cache hit/miss statistics, you would simply call `.cache_info()` on the decorated function.

For example, using a recursive Fibonacci implementation to maximize cache hit/miss:

```python
from safecache import safecache

@safecache()
def fib(n):
    if n <= 1:
        return n
    return fib(n-1) + fib(n-2)

fib(100)
fib.cache_info()  # CacheInfo(hits=98, misses=101, maxsize=128, currsize=101)
```

## Inspecting the Cache Without Calling the Function

`.cache_get(*args, **kwargs)` looks up a call's cached value directly,
without invoking the wrapped function. It raises `safecache.CacheMiss` if
nothing has ever been cached for those arguments, or `safecache.CacheExpired`
if an entry exists but its TTL has elapsed. Both are subclasses of
`safecache.CacheError` (and of the more specific built-in `KeyError` /
`ValueError`, respectively), so either can be caught narrowly or together.
A successful lookup counts as a hit and updates the LRU order, exactly like
a normal call would:

```python
from safecache import safecache, CacheError

@safecache(ttl=60)
def fib(n):
    if n <= 1:
        return n
    return fib(n-1) + fib(n-2)

try:
    fib.cache_get(10)
except CacheError:
    fib(10)  # populate it
```

## Why safecache?

[Caching](https://en.wikipedia.org/wiki/Cache_(computing)) using native Python can be useful to minimize the caching latency (e.g. [dynamic programming problems](https://en.wikipedia.org/wiki/Dynamic_programming#Examples:_Computer_algorithms)), but it could be used or implemented incorrectly to result in inconsistent caching behaviors and bugs. For example, here is a scenario where one needs object integrity - but does not have that guarantee due to cache contamination.

```python
from functools import lru_cache

@lru_cache()
def convert_to_list(x):
    return [x]

converted = convert_to_list(1)  # [1]

# if we mutate the variable:
converted.append(2)  # [1, 2]

# then the referenced, origin cache is also mutated.
# We naturally expect this result to still be [1].
convert_to_list(1)  # [1, 2]

# this is because both `converted` and the function
# object references the same memory address.
assert hex(id(convert_to_list(1))) == hex(id(converted))  # 0x7be3da4ca7c8
```

As you can see, `.append` has contaminated our mutable cache storage inside the [lru_cache](https://docs.python.org/3/library/functools.html#functools.lru_cache) (which is due to the fundamentals of [Python object referencing](https://docs.python.org/2.0/ref/objects.html)). **safecache** solves this by heuristically identifying which cached object are mutable and guarding them by returning their (deep)copies. As expected, immutable caches are not copied as they do not need to be.

In most cases, [lru_cache](https://docs.python.org/3/library/functools.html#functools.lru_cache) is a great way to cache expensive results in Python; but if you need stringent thread-safe cache integrity preservation , you will definitely find **safecache** useful.

## License

**safecache** is under [Apache 2.0 license](./LICENSE).