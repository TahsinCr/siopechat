"""Shared private SQLite connections and transaction handling."""

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from .process import set_private_permissions


class SQLiteRepositoryBase:
    """Own a private database file and provide short-lived connections."""

    def __init__(self, path, *, timeout=5, error_message=None):
        self.path = Path(path)
        self.timeout = timeout
        self.error_message = error_message

    def prepare_private_database(self, *, directory_mode=0o700, file_mode=0o600):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=directory_mode)
        descriptor = os.open(self.path, os.O_CREAT | os.O_RDWR, file_mode)
        os.close(descriptor)
        set_private_permissions(self.path, file_mode)

    def configure_connection(self, connection):
        """Apply repository-specific PRAGMAs."""

    @contextmanager
    def connect(self):
        connection = None
        try:
            connection = sqlite3.connect(self.path, timeout=self.timeout)
            self.configure_connection(connection)
            with connection:
                yield connection
        except sqlite3.Error as error:
            if self.error_message is None:
                raise
            raise OSError(self.error_message) from error
        finally:
            if connection is not None:
                connection.close()

    @contextmanager
    def transaction(self, *, immediate=False):
        with self.connect() as connection:
            if immediate:
                connection.execute("BEGIN IMMEDIATE")
            yield connection
