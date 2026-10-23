"""API keys in Windows Credential Manager (via keyring), never in settings.json or the repository."""
SERVICE = 'Alfred'


def _backend():
    import keyring
    return keyring


def get(name):
    try:
        return _backend().get_password(SERVICE, name) or ''
    except Exception:  # no usable credential store: behave as "no key"
        return ''


def has(name):
    return bool(get(name))


def put(name, value):
    """Store (or, with an empty value, remove) a key. Raises RuntimeError when it can't be stored."""
    value = (value or '').strip()
    if not value:
        delete(name)
        return
    try:
        _backend().set_password(SERVICE, name, value)
    except Exception as error:
        raise RuntimeError(f'Couldn’t save the key in Windows Credential Manager: {error}') from None


def delete(name):
    try:
        _backend().delete_password(SERVICE, name)
    except Exception:
        pass
