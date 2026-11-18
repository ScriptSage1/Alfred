"""API keys in Windows Credential Manager (via keyring), never in settings.json or the repository."""
SERVICE = 'Juno'
OLD_SERVICE = 'Alfred'  # Juno's name until November 2026: a key saved under it moves over the first time it is read


def _backend():
    import keyring
    return keyring


def get(name):
    try:
        value = _backend().get_password(SERVICE, name)
        if not value:
            value = _move_from_alfred(name)
        return value or ''
    except Exception:  # no usable credential store: behave as "no key"
        return ''


def _move_from_alfred(name):
    """A key saved before the rename: stored again under Juno, and the old copy removed. Returns the key or None."""
    value = _backend().get_password(OLD_SERVICE, name)
    if value:
        try:
            _backend().set_password(SERVICE, name, value)
            _backend().delete_password(OLD_SERVICE, name)
        except Exception:  # still usable this time; moved on a later read
            pass
    return value


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
    for service in (SERVICE, OLD_SERVICE):
        try:
            _backend().delete_password(service, name)
        except Exception:
            pass
