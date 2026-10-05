"""Jev chooses an observed action. Code owns execution."""

__all__ = ["Agent", "Browser"]


def __getattr__(name):
    if name == "Agent":
        from .agent import Agent

        return Agent
    if name == "Browser":
        from .browser import Browser

        return Browser
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
