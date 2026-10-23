"""PLACEHOLDER: chatting with your documents (retrieve passages, then a cited answer from the local
chat model) is the next feature. Until then Ask Alfred explains that it isn't ready yet.
"""
from .ai import NotConfigured

NOT_READY = 'Chatting with your documents is coming in the next update. Search (by keyword and by meaning) already works.'


def ask(index, settings, question, history=(), on_token=lambda t: None, cancel=None, **options):
    raise NotConfigured(NOT_READY)
