import hmac


def authorize(handler, config):
    """Optional bearer auth: local development is open; production can require a token."""
    if not config.auth_token:
        return True
    supplied=handler.headers.get('Authorization','')
    return hmac.compare_digest(supplied, 'Bearer '+config.auth_token)
