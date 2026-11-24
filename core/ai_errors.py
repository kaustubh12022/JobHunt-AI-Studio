"""Shared AI failure signals used by the locked studio pipeline."""

from __future__ import annotations


class CredentialsRequired(RuntimeError):
    """The DeepSeek credential is missing, rejected, or out of balance."""


class RunPaused(Exception):
    """The operator paused the run or its worker lease was lost."""
