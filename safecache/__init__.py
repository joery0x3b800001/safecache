# Copyright 2020 Verizon Inc.
# Licensed under the terms of the Apache License 2.0.
# See LICENSE file in project root for terms.

"""
safecache
=========
"""

from .exceptions import CacheExpired
from .exceptions import CacheMiss

from .safecache import IMMUTABLE_TYPES
from .safecache import is_immutable
from .safecache import is_mutable
from .safecache import mutabletypeguard
from .safecache import safecache

try:
    from importlib.metadata import PackageNotFoundError, version
    __version__ = version("safecache")
except PackageNotFoundError:
    __version__ = "0.0.0"
