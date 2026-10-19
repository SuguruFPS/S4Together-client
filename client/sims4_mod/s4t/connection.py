"""Connexion bornée ; le thread réseau ne touche jamais services ni données du jeu."""
from collections import deque
import json
import queue
import select
import socket
import threading
import time
import uuid

from .config import ConfigStore
from .websocket import TlsUnavailable, WebSocket, ssl, validate_endpoint
from .journal import log

# Attente réseau maximale d'une requête mise en file avant son envoi (était 100 ms).
NETWORK_WAIT_SECONDS = 0.02


def classify_failure(failure):
    """Code d'erreur stable, sans texte d'exception ni endpoint."""
    if isinstance(failure, TlsUnavailable):
        return 'tls_unavailable_in_game'
    if isinstance(failure, socket.gaierror):
        return 'dns_failure'
    if ssl is not None and isinstance(failure, ssl.SSLError):
        return 'tls_failure'
    if isinstance(failure, (socket.timeout, TimeoutError)):
        return 'connection_timeout'
    if isinstance(failure, ConnectionRefusedError):
        return 'connection_refused'
    if isinstance(failure, (EOFError, ConnectionResetError)):
        return 'connection_interrupted'
    return 'network_or_protocol_failure'


class ByteBoundedQueue(queue.Queue):
    """Borne cumulée16Mio en plus du nombre ; hooks sous le mutex de Queue."""
    def _init(self, maxsize):
        super()._init(maxsize)
        self.used_bytes = 0

    def _put(self, item):
        size = len(json.dumps(item, ensure_ascii=False).encode('utf-8'))
        if self.used_bytes + size > 16 * 1024 * 1024:
            raise queue.Full
        super()._put((item, size)); self.used_bytes += size

    def _get(self):
        item, size = super()._get()
        self.used_bytes -= size
        return item


class ConnectionManager:
    def __init__(self, config=None, connector=None, queue_size=128):
        self.config = config or ConfigStore()
        self.connector = connector
        self.active_connector = connector or WebSocket.connect
        self.thread_id = threading.get_ident()
        self.incoming = ByteBoundedQueue(queue_size)
        self.outgoing = queue.Queue(queue_size)
        self.pending = {}
        self.history = deque(maxlen=100)
        self.snapshot = {}
        self.objects = {}
        self.season_sequence = -1
        self.identity = {}
        self.endpoint = None
        self.hello_id = None
        self.worker = None
        self.transport = None
        self.stop = threading.Event()
        self.authenticated = False
        self.phase = 'disconnected'
        self.error = None

    def _main(self):
        if threading.get_ident() != self.thread_id:
            raise RuntimeError('Traitement requis sur le thread principal')

    def connect(self, endpoint=None, name=None, password=''):
        self._main()
        self.disconnect()
        settings = self.config.settings()
        endpoint = endpoint or settings['endpoint']
        validate_endpoint(endpoint, allow_lan=settings.get('allow_lan') is True)
        if not isinstance(password, str) or len(password) > 128:
            raise ValueError('Mot de passe invalide')
        self.active_connector = self.connector or (lambda url: WebSocket.connect(url, allow_lan=settings.get('allow_lan') is True))
        display_name = name or settings['name']
        if not isinstance(display_name, str) or not 1 <= len(display_name.strip()) <= 40:
            raise ValueError('Nom joueur invalide')
        if self.endpoint != endpoint:
            self.pending.clear()
        self.endpoint = endpoint
        self.identity = self.config.identity(endpoint)
        self.snapshot = {}
        self.objects = {}
        self.season_sequence = -1
        self.history.clear()
        self._drain(self.incoming)
        self._drain(self.outgoing)
        self.stop = threading.Event()
        self.error = None
        self.phase = 'connecting'
        self.hello_id = str(uuid.uuid4())
        payload = {'name': display_name, 'dlcs': settings['dlcs'], 'transportChunking': 1}
        if password:
            payload['password'] = password
        payload.update(self.identity)
        hello = {'v': 1, 'id': self.hello_id, 'type': 'hello', 'payload': payload}
        self.worker = threading.Thread(target=self._network, args=(hello,), name='S4Together-network')
        self.worker.daemon = True
        self.worker.start()
        return self.hello_id

    @staticmethod
    def _drain(channel):
        while True:
            try:
                channel.get_nowait()
            except queue.Empty:
                return

    def _network(self, hello):
        from .chunks import Assembler
        assembler = Assembler()
        transport = None
        try:
            transport = self.active_connector(self.endpoint)
            self.transport = transport
            transport.send_text(json.dumps(hello, separators=(',', ':')))
            self.phase = 'awaiting_hello'
            last_ping = time.monotonic()
            hello_deadline = last_ping + 10
            hello_received = self.authenticated
            while not self.stop.is_set():
                if assembler.expired:
                    raise TimeoutError('Transmission découpée expirée')
                if not hello_received and time.monotonic() > hello_deadline:
                    raise TimeoutError('Réponse HELLO dépassée')
                for _ in range(16):
                    try:
                        request = self.outgoing.get_nowait()
                    except queue.Empty:
                        break
                    transport.send_text(json.dumps(request, separators=(',', ':')))
                if time.monotonic() - last_ping > 10:
                    transport.send_text(json.dumps({'v': 1, 'id': str(uuid.uuid4()), 'type': 'ping', 'payload': {}}))
                    last_ping = time.monotonic()
                if transport.buffer or select.select([transport.socket], [], [], NETWORK_WAIT_SECONDS)[0]:
                    message = assembler.accept(transport.receive_text())
                    if message is None:
                        continue
                    if not isinstance(message, dict) or message.get('v') != 1:
                        raise ValueError('Message protocole invalide')
                    if message.get('id') == hello.get('id'):
                        hello_received = True
                    self.incoming.put_nowait(message)
        except Exception as failure:
            # Ni texte d'exception ni endpoint/identité/token dans le journal du jeu.
            if not self.stop.is_set():
                self.error = classify_failure(failure)
                log('network', self.error)
        finally:
            if transport is not None:
                transport.close()
            self.transport = None
            self.phase = 'disconnected'

    def request(self, request_type, payload=None, request_id=None):
        self._main()
        if not self.authenticated or self.transport is None or self.phase == 'disconnected':
            raise RuntimeError('Connexion authentifiée requise ; exécuter s4t.poll')
        request_id = request_id or str(uuid.uuid4())
        try:
            uuid.UUID(request_id)
        except (ValueError, TypeError, AttributeError):
            raise ValueError('Identifiant requête invalide')
        request = {'v': 1, 'id': request_id, 'type': request_type, 'payload': payload or {}}
        if request_type not in ('local.save-context', 'snapshot', 'chat', 'object.set', 'ping', 'ack', 'character.resume', 'character.bind', 'character.checkpoint',
                                'game.join', 'game.state', 'game.leave', 'game.snapshot', 'game.appearance.publish', 'game.appearance.list', 'game.appearance.get', 'game.visibility.report',
                                'game.interaction', 'game.speed.get', 'game.speed.set', 'terrain.publish', 'terrain.get', 'terrain.catalog',
                                'property.claim', 'property.release', 'property.move',
                                'business.claim', 'business.release'):
            raise ValueError('Commande mod non autorisée')
        if len(json.dumps(request).encode('utf-8')) > 65536:
            raise ValueError('Requête trop grande')
        old = self.pending.get(request_id)
        if old is not None and old != request:
            raise ValueError('UUID déjà utilisé avec une autre requête')
        if old is None and len(self.pending) >= 128:
            raise RuntimeError('Trop de requêtes en attente')
        try:
            self.outgoing.put_nowait(request)
        except queue.Full:
            raise RuntimeError('File réseau pleine')
        self.pending[request_id] = request
        return request_id

    def forget(self, request_id):
        """Abandonne une requête sans réponse attendue (présence périmée) : elle ne sera jamais rejouée."""
        self._main()
        self.pending.pop(request_id, None)

    def retry(self, request_id):
        self._main()
        request = self.pending.get(request_id)
        if request is None:
            raise ValueError('Requête en attente inconnue')
        return self.request(request['type'], request['payload'], request_id)

    def observe(self, observation):
        self._main()
        if any(r['type'] == 'object.set' for r in self.pending.values()):
            raise RuntimeError('Observation précédente encore en attente')
        object_id = 's4t-observation-' + self.identity.get('playerId', '')
        current = self.objects.get(object_id, {})
        return self.request('object.set', {'objectId': object_id, 'expectedRevision': current.get('revision', 0), 'value': observation})

    def poll(self, limit=32):
        self._main()
        if not isinstance(limit, int) or not 1 <= limit <= 128:
            raise ValueError('Limite poll invalide')
        delivered = []
        for _ in range(limit):
            try:
                message = self.incoming.get_nowait()
            except queue.Empty:
                break
            try:
                message = self._apply(message)
            except (ValueError, KeyError, TypeError):
                self.error = 'protocol_state_failure'
                self.disconnect()
                delivered.append({'v': 1, 'type': 'error', 'ok': False, 'code': self.error})
                break
            # Le dernier snapshot est déjà conservé dans self.snapshot ; l'historique
            # ne doit pas garder100 copies de messages pouvant maintenant atteindre16Mio.
            size = len(json.dumps(message, ensure_ascii=False).encode('utf-8'))
            self.history.append(message if size <= 65536 else
                                dict((k, message[k]) for k in ('v','type','id','ok') if k in message))
            delivered.append(message)
            if self.error in ('identity_persistence_failed', 'hello_rejected'):
                break
        return delivered

    @staticmethod
    def _number(value, minimum=0):
        return type(value) is int and minimum <= value <= 9223372036854775807

    @classmethod
    def _valid_object(cls, obj):
        if (not isinstance(obj, dict) or not isinstance(obj.get('id'), str) or not obj['id'] or
                not isinstance(obj.get('ownerId'), str) or not obj['ownerId'] or 'value' not in obj or
                not cls._number(obj.get('revision'), 1)):
            raise ValueError('Objet invalide')

    @classmethod
    def _valid_snapshot(cls, payload):
        if (not isinstance(payload, dict) or not isinstance(payload.get('worldId'), str) or not payload['worldId'] or
                len(payload['worldId']) > 100 or not cls._number(payload.get('sequence'))):
            raise ValueError('Snapshot invalide')
        for name in ('players', 'households', 'lots', 'objects'):
            if not isinstance(payload.get(name), list) or len(payload[name]) > 4096:
                raise ValueError('Collection snapshot invalide')
        seen = set()
        for obj in payload['objects']:
            cls._valid_object(obj)
            if obj['id'] in seen:
                raise ValueError('Objet snapshot dupliqué')
            seen.add(obj['id'])
        for player in payload['players']:
            if not isinstance(player, dict) or not isinstance(player.get('playerId'), str) or not isinstance(player.get('name'), str):
                raise ValueError('Joueur snapshot invalide')
        for name in ('households', 'lots'):
            if any(not isinstance(item, dict) or not isinstance(item.get('id'), str) for item in payload[name]):
                raise ValueError('Référence snapshot invalide')

    def _apply(self, message):
        if not isinstance(message, dict) or type(message.get('v')) is not int or message['v'] != 1 or not isinstance(message.get('type'), str):
            raise ValueError('Enveloppe invalide')
        request_id = message.get('id')
        payload = message.get('payload')
        if request_id is not None and ((message['type'], message.get('ok')) not in (('ack', True), ('error', False)) or type(message.get('ok')) is not bool):
            raise ValueError('Réponse invalide')
        if request_id == self.hello_id:
            if message.get('ok') is not True:
                self.error = 'hello_rejected'; self.disconnect()
                code = 'invalid_password' if message.get('code') == 'invalid_password' else 'hello_rejected'
                log('admission', code)
                return {'v': 1, 'type': 'hello', 'ok': False, 'code': code}
            if (message['type'] != 'ack' or not isinstance(payload, dict) or
                    any(not isinstance(payload.get(key), str) or not payload[key] or len(payload[key]) > 512
                        for key in ('playerId', 'token', 'sessionId'))):
                raise ValueError('HELLO invalide')
            self._valid_snapshot(payload.get('snapshot'))
            try:
                self.config.save_identity(self.endpoint, payload['playerId'], payload['token'])
            except (ValueError, OSError):
                self.error = 'identity_persistence_failed'; self.disconnect()
                return {'v': 1, 'type': 'hello', 'ok': False, 'code': self.error}
            self.identity = {'playerId': payload['playerId'], 'token': payload['token']}
            self.launcher_bridge = payload['sessionId'].startswith('bridge-')
            self._snapshot(payload['snapshot'])
            self.authenticated = True
            if self.transport is not None:
                self.phase = 'connected'
            return {'v': 1, 'type': 'hello', 'ok': True, 'id': request_id}
        if not self.authenticated:
            raise ValueError('Message avant HELLO valide')
        if request_id in self.pending:
            request = self.pending[request_id]
            if message.get('ok') is True and request['type'] == 'snapshot':
                self._snapshot(payload)
            elif message.get('ok') is True and request['type'] == 'object.set':
                if (not isinstance(payload, dict) or payload.get('objectId') != request['payload']['objectId'] or
                        not self._number(payload.get('sequence')) or not self._number(payload.get('revision'), 1)):
                    raise ValueError('ACK objet invalide')
                obj = {'id': payload['objectId'], 'ownerId': self.identity['playerId'],
                       'value': request['payload']['value'], 'revision': payload['revision'], 'sequence': payload['sequence']}
                self._object(obj)
            self.pending.pop(request_id)
        elif message['type'] == 'snapshot':
            self._snapshot(payload)
        elif message['type'] == 'object':
            self._object(payload)
        elif message['type'] in ('season.state', 'season.chosen'):
            # Diffusion saison #138 : {worldId, sequence, season}. Une séquence plus ancienne est ignorée.
            if (isinstance(payload, dict) and payload.get('worldId') == self.snapshot.get('worldId') and
                    self._number(payload.get('sequence')) and isinstance(payload.get('season'), dict) and
                    payload['sequence'] >= self.season_sequence):
                self.snapshot['season'] = payload['season']
                self.season_sequence = payload['sequence']
        elif message['type'] == 'players':
            if not isinstance(payload, dict) or not isinstance(payload.get('players'), list):
                raise ValueError('Événement joueurs invalide')
            self.snapshot['players'] = payload['players']
        return message

    def _object(self, obj):
        self._valid_object(obj)
        if not self._number(obj.get('sequence')):
            raise ValueError('Séquence objet invalide')
        current = self.objects.get(obj['id'])
        if current is not None and current['revision'] >= obj['revision']:
            return
        if current is None and len(self.objects) >= 1000:
            raise ValueError('Limite objets mod atteinte')
        self.objects[obj['id']] = obj
        self.snapshot['sequence'] = max(self.snapshot.get('sequence', 0), obj['sequence'])

    def _snapshot(self, payload):
        self._valid_snapshot(payload)
        if self.snapshot:
            if payload['worldId'] != self.snapshot['worldId']:
                raise ValueError('Monde inattendu')
            if payload['sequence'] < self.snapshot['sequence']:
                return
            if not set(self.objects).issubset({obj['id'] for obj in payload['objects']}):
                raise ValueError('Disparition objet non supportée par protocole v1')
            if any(obj['id'] in self.objects and obj['revision'] < self.objects[obj['id']]['revision'] for obj in payload['objects']):
                raise ValueError('Révision snapshot rétrograde')
        season = self.snapshot.get('season')
        self.snapshot = payload
        if payload['sequence'] < self.season_sequence and season is not None:
            self.snapshot['season'] = season
        else:
            self.season_sequence = payload['sequence']
        self.objects = {obj['id']: obj for obj in payload['objects'][:1000]}

    def world_season(self):
        """(worldId, objet saison v1 ou None) du monde serveur connu."""
        return self.snapshot.get('worldId'), self.snapshot.get('season')

    def status(self):
        self._main()
        return {'state': self.phase, 'authenticated': self.authenticated and self.transport is not None,
                'pending': len(self.pending), 'incoming': self.incoming.qsize(), 'error': self.error,
                'objects': len(self.objects), 'gameReadiness': 'NOT_TESTED_GAME',
                'message': {'dns_failure': 'Adresse du serveur introuvable.',
                    'tls_failure': 'Certificat TLS refusé.',
                    'tls_unavailable_in_game': 'wss:// impossible dans le jeu (pas de module ssl) : utiliser l’IP LAN du serveur.',
                    'connection_timeout': 'Serveur injoignable dans le délai réseau.',
                    'connection_refused': 'Connexion TCP refusée : vérifier le serveur et le port.',
                    'connection_interrupted': 'Connexion interrompue : utiliser s4t.connect puis s4t.poll.',
                    'hello_rejected': 'Admission refusée : vérifier le mot de passe et s4t.poll.'}.get(self.error, '')}

    def disconnect(self):
        self._main()
        self.stop.set()
        if self.transport is not None:
            self.transport.close()
        if self.worker is not None and self.worker.is_alive():
            self.worker.join(timeout=0.2)
            if self.worker.is_alive():
                # Un handshake DNS/TLS encore bloqué ne doit pas être remplacé par un second worker.
                raise RuntimeError('Connexion précédente en cours de fermeture ; réessayer après le délai réseau')
        self.authenticated = False
        self.phase = 'disconnected'
