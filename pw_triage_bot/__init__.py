"""Triage and housekeeping for a patchwork project."""

from __future__ import annotations

from importlib.metadata import version

# What the tool calls itself: its command, the directories and files it defaults
# to, the credential it reads, and how it introduces itself to patchwork.
NAME = __package__.replace("_", "-")
VERSION = version(NAME)


class Error(Exception):
    """A failure the tool can name."""
