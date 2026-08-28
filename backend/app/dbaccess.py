"""Thread-safe access to a shared in-memory DuckDB database.

A DuckDB connection object is not safe to use from several threads at once.
Parameter binding is per-connection state, so two concurrent queries interleave
their bindings and one receives the other's parameters. The failure is not a
clean error either -- it surfaces as a nonsense cast, such as a category name
arriving in a date slot:

    ConversionException: Could not convert string 'Bulky' to INT32
    ... WHERE CAST(p.date AS DATE) >= ? ... AND p.category = ?

This matters here because the API serves synchronous endpoints from a thread
pool and the dashboard fires several fetches in parallel on load. The bug is
intermittent, load-dependent, and would surface during a live demo rather than
in a test run.

`conn.cursor()` returns a fresh connection over the same database, which is the
supported way to work from multiple threads. This wrapper hands each thread its
own cursor and proxies the connection API, so every existing `self.conn.execute`
call site keeps working unchanged.
"""

from __future__ import annotations

import threading
from typing import Any

import duckdb


class ThreadSafeConnection:
    """Proxies DuckDB, giving each thread its own cursor over one database."""

    def __init__(self, database: str = ":memory:") -> None:
        # The root connection owns the database; it is never used for queries
        # directly, only to spawn per-thread cursors.
        self._root = duckdb.connect(database)
        self._local = threading.local()
        self._lock = threading.Lock()

    @property
    def _cursor(self) -> duckdb.DuckDBPyConnection:
        cursor = getattr(self._local, "cursor", None)
        if cursor is None:
            # cursor() itself touches shared state, so creation is serialised.
            with self._lock:
                cursor = self._root.cursor()
            self._local.cursor = cursor
        return cursor

    # -- proxied surface ---------------------------------------------------

    def execute(self, query: str, parameters: Any = None):
        return self._cursor.execute(query, parameters) if parameters else self._cursor.execute(query)

    def register(self, name: str, frame: Any) -> None:
        """Register a Python object as a view on the root connection.

        DuckDB scopes registered objects to the connection that registered them,
        so a cursor cannot see them. Anything that reads a registered frame must
        therefore run through `bootstrap_execute` on the same connection --
        which is why loading happens once at startup and only real tables are
        visible to threads afterwards.
        """
        with self._lock:
            self._root.register(name, frame)

    def bootstrap_execute(self, query: str, parameters: Any = None):
        """Run a statement on the root connection.

        For startup only: creating the real tables that every thread will read.
        Never use this to serve a request -- that is the shared-state path this
        class exists to avoid.
        """
        with self._lock:
            return (
                self._root.execute(query, parameters)
                if parameters
                else self._root.execute(query)
            )

    def unregister(self, name: str) -> None:
        with self._lock:
            self._root.unregister(name)

    def close(self) -> None:
        with self._lock:
            self._root.close()

    def __getattr__(self, item: str) -> Any:
        # Anything not explicitly proxied falls through to the thread's cursor.
        return getattr(self._cursor, item)
