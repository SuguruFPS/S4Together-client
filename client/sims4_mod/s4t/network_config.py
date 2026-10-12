"""Configuration explicite ; aucune activation du réseau ou ouverture de port."""
from urllib.parse import urlsplit, urlunsplit
from .websocket import is_lan, validate_endpoint


def endpoint_for(address, port):
    if not isinstance(address, str) or not address.strip():
        raise ValueError('Saisir l’adresse du serveur')
    number = int(port)
    if not 1 <= number <= 65535:
        raise ValueError('Port invalide')
    address = address.strip()
    if '://' not in address:
        if not is_lan(address):
            raise ValueError('Hors LAN, adresse wss:// requise')
        return 'ws://{}:{}/ws'.format(address, number), True
    parsed, _ = validate_endpoint(address, allow_lan=True)
    if parsed.query:
        raise ValueError('Paramètres dans l’adresse interdits')
    host = parsed.hostname
    if ':' in host:
        host = '[' + host + ']'
    endpoint = urlunsplit((parsed.scheme, '{}:{}'.format(host, number), parsed.path or '/ws', '', ''))
    lan = parsed.scheme == 'ws' and is_lan(parsed.hostname)
    validate_endpoint(endpoint, allow_lan=lan)
    return endpoint, lan
