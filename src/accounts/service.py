"""Assemble the account service from its focused operation groups."""

from .core import AccountsCore
from .history import AccountsHistory
from .projects import AccountsProjects
from .sending import AccountsSending
from .sessions import AccountsSessions
from .transfers import AccountsTransfers


class Accounts(
    AccountsCore,
    AccountsSessions,
    AccountsHistory,
    AccountsSending,
    AccountsTransfers,
    AccountsProjects,
):
    """Coordinate account-scoped provider operations."""
