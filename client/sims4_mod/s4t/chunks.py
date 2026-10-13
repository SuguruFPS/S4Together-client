"""Réassemblage réseau borné, Python3.7 ; rien n'est appliqué avant le dernier bloc validé."""
import base64
import hashlib
import json
import time
import uuid

BLOCK, THRESHOLD, MAX_BYTES = 32768, 49152, 16 * 1024 * 1024


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError('Champ transport dupliqué')
        result[key] = value
    return result


class Assembler:
    def __init__(self, clock=time.monotonic):
        self.clock, self.started, self.meta, self.next, self.data = clock, None, None, 0, bytearray()

    @property
    def expired(self):
        return self.started is not None and self.clock() - self.started > 30

    def accept(self, text):
        if len(text.encode('utf-8')) > 65536 or self.expired:
            raise ValueError('Transmission trop grande ou expirée')
        message = json.loads(text, object_pairs_hook=_pairs)
        if not isinstance(message, dict) or message.get('type') != 'transport.chunk':
            if self.meta is not None:
                raise ValueError('Transmission interrompue')
            return message
        if set(message) != {'v', 'type', 'payload'} or type(message['v']) is not int or message['v'] != 1:
            raise ValueError('Enveloppe transport invalide')
        p = message['payload']
        if not isinstance(p, dict) or set(p) != {'transferId','index','total','size','sha256','data'}:
            raise ValueError('Métadonnées transport invalides')
        if any(type(p[n]) is not int for n in ('index','total','size')):
            raise ValueError('Entiers transport invalides')
        if (not isinstance(p['transferId'], str) or str(uuid.UUID(p['transferId'])) != p['transferId']
                or not isinstance(p['sha256'], str) or len(p['sha256']) != 64
                or any(c not in '0123456789abcdef' for c in p['sha256'])
                or not THRESHOLD < p['size'] <= MAX_BYTES
                or p['total'] != (p['size']+BLOCK-1)//BLOCK or not 0 <= p['index'] < p['total']
                or not isinstance(p['data'], str) or not 0 < len(p['data']) <= 43692):
            raise ValueError('Bornes transport invalides')
        block = base64.b64decode(p['data'], validate=True)
        if len(block) != min(BLOCK,p['size']-p['index']*BLOCK) or base64.b64encode(block).decode('ascii') != p['data']:
            raise ValueError('Bloc transport invalide')
        meta = (p['transferId'], p['total'], p['size'], p['sha256'])
        if self.meta is None:
            if p['index'] != 0:
                raise ValueError('Premier bloc manquant')
            self.meta, self.started = meta, self.clock()
        if meta != self.meta or p['index'] != self.next:
            raise ValueError('Ordre ou identité transport invalide')
        self.data.extend(block); self.next += 1
        if self.next < p['total']:
            return None
        if len(self.data) != p['size'] or hashlib.sha256(self.data).hexdigest() != p['sha256']:
            raise ValueError('Empreinte transport invalide')
        complete = json.loads(self.data.decode('utf-8'), object_pairs_hook=_pairs)
        if not isinstance(complete, dict) or complete.get('type') == 'transport.chunk':
            raise ValueError('Message logique invalide')
        self.meta, self.started, self.next, self.data = None, None, 0, bytearray()
        return complete
