"""Exceptions shared across stack-pr's modules."""

from __future__ import annotations


class StackPRError(Exception):
    """An error to report to the user, without a traceback.

    ``main()`` catches it, runs its usual cleanup, prints the message as an
    ``ERROR:`` and exits with status 1. Raise it for failures the user can act
    on; let anything unexpected propagate as its own exception.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message
