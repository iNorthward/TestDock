"""Explicit ContextVar handoff at worker boundaries; one context per task."""
from contextvars import copy_context


def context_callable(function, *args, **kwargs):
    context = copy_context()
    return lambda: context.run(function, *args, **kwargs)


def submit_with_context(executor, function, *args, **kwargs):
    return executor.submit(context_callable(function, *args, **kwargs))
