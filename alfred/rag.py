"""PLACEHOLDER: answering questions from your files is not part of Alfred yet.

ui.py shows NotConfigured as a "set up AI" message. Replace this file with the real module later.
"""
from .ai import NOT_READY, NotConfigured


def ask(index, settings, question, history=(), on_token=lambda t: None, cancel=None, **options):
    raise NotConfigured(NOT_READY)
