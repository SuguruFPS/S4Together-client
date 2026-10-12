"""Publication des événements s4t.interaction au serveur et télémétrie reçue des autres joueurs. Logique pure.

Requête : `game.interaction {streamId, event}`. `streamId` est le flux SERVEUR confirmé par l'ACK de game.join
(PresenceSync.stream_id) ; `event.streamId` reste le flux local de l'observateur, distinct. Une seule requête en
vol ; sa charge et son UUID sont figés pour tout renvoi. Aucune connexion n'est créée ici, aucun join n'est
relancé et aucun flux n'est renouvelé : le publisher suit le contexte que lui donne AutoLink.

Les événements `game.interaction` reçus sont de la télémétrie : validés, dédoublonnés, gardés en nombre borné
pour le diagnostic. Rien n'est rejoué : ce module n'importe aucune API du jeu. NOT_TESTED_GAME.
"""
import json
import re
import time
import uuid
from collections import OrderedDict, deque

from .interaction_observer import MAX_EVENTS, MAX_SEQ, _STREAM, _player, _text, clean_event

REQUEST = 'game.interaction'
ACK_TIMEOUT = 10.0       # Secondes sans réponse avant de renvoyer la même requête.
MAX_ATTEMPTS = 3         # Envois d'une même requête sans réponse avant abandon.
RATE_RETRY = 1.0         # Secondes après un refus rate_limited, comptées depuis la réception du refus.
MAX_RATE_RETRIES = 3
HOLD = 10.0              # Pause après game_join_required / stale_stream : pas de rafale de refus.
RECEIVED = 8
MAX_STREAMS = 64
MAX_RECIPIENTS = 64
MAX_CODES = 16

_CODE = re.compile(r'[a-z0-9_.]{1,64}')
# Le serveur ne connaît plus ce flux : les événements relevés pour lui n'ont plus de destinataire.
STREAM_CODES = ('game_join_required', 'stale_stream')
_OBSERVATION = frozenset(('playerId', 'characterId', 'zoneId', 'streamId', 'revision', 'event'))


def _revision(value):
    return type(value) is int and 0 < value < MAX_SEQ


def clean_observation(payload, own, zone, allow_own=False):
    """Observation reçue du serveur, strictement validée, ou None.

    `own` : joueur local ; `zone` : zone native courante. L'émetteur est un autre joueur de la même zone, le
    joueur local est destinataire, et une cible `remote_clone` ne désigne jamais l'émetteur lui-même.
    """
    if type(payload) is not dict or set(payload) != {'observation', 'recipientPlayerIds'} or not _player(own):
        return None
    observation, recipients = payload['observation'], payload['recipientPlayerIds']
    if (type(recipients) is not list or not 0 < len(recipients) <= MAX_RECIPIENTS
            or not all(_player(player) for player in recipients) or own not in recipients):
        return None
    if type(observation) is not dict or set(observation) != _OBSERVATION:
        return None
    player, event = observation['playerId'], clean_event(observation['event'])
    if (not _player(player) or player == own and not allow_own or not _text(observation['characterId'], _STREAM)
            or type(zone) is not str or type(observation['zoneId']) is not str or observation['zoneId'] != zone
            or not _text(observation['streamId'], _STREAM) or not _revision(observation['revision']) or event is None):
        return None
    if event['target']['kind'] == 'remote_clone' and event['target']['playerId'] == player:
        return None
    return {'playerId': player, 'characterId': observation['characterId'], 'zoneId': zone,
            'streamId': observation['streamId'], 'revision': observation['revision'], 'event': event}


class InteractionPublisher:
    """File bornée, une requête en vol, refus comptés par code. `manager` est passé à chaque appel.

    `tick(manager, authenticated, flow, events)` : `flow` vaut (personnage, zone native, flux serveur) quand le
    personnage est lié et la présence confirmée, sinon None. Tout changement de `flow` oublie la requête en vol
    et purge la file.
    """

    def __init__(self, new_id=None, clock=time.monotonic, peer_streams=None):
        self.new_id = new_id if new_id is not None else lambda: str(uuid.uuid4())
        self.clock = clock
        self.peer_streams = peer_streams
        self._clear()

    def _clear(self):
        self.flow = None
        self.queue = deque()
        self.flight = None
        self.unsupported = False
        self.hold_until = None
        self.received = deque(maxlen=RECEIVED)
        self.seen = OrderedDict()
        self.sequences = OrderedDict()
        self.refused = {}
        self.code = self.revision = None
        self.published = self.dropped = self.invalid = self.retries = self.send_errors = 0
        self.rate_limited = self.ack_invalid = 0
        self.received_count = self.rejected = self.duplicates = self.ignored = 0

    def in_flight(self):
        return self.flight is not None

    def reset(self, manager=None):
        """Oublie la requête en vol, la file et la télémétrie. Sans gestionnaire : oubli local seulement."""
        flight = self.flight
        self._clear()
        if flight is not None and manager is not None:
            manager.forget(flight['id'])

    @staticmethod
    def _flow(flow):
        if type(flow) is not tuple or len(flow) != 3:
            return None
        character, zone, stream = flow
        if type(character) is not str or type(zone) is not str or not _text(stream, _STREAM):
            return None
        return flow

    def tick(self, manager, authenticated, flow, events=()):
        """Rend 'game.interaction' si une requête part à ce tick, sinon None."""
        flow = self._flow(flow) if authenticated is True else None
        if flow != self.flow:
            self.reset(manager)
            self.flow = flow
        if flow is None:
            return None
        self._confirmed_peers()
        now = self.clock()
        if self.hold_until is not None and now >= self.hold_until:
            self.hold_until = None
        if self.unsupported or self.hold_until is not None:
            self.dropped += len(events)
            return None
        for event in events:
            self._offer(event)
        if self.flight is None:
            if not self.queue:
                return None
            # Charge figée en texte : ni l'événement d'origine ni le gestionnaire ne peuvent la modifier.
            body = json.dumps({'streamId': flow[2], 'event': self.queue.popleft()}, sort_keys=True)
            self.flight = {'id': self.new_id(), 'body': body, 'sent': False, 'sentAt': None, 'retryAt': None,
                           'attempts': 0, 'rate': 0}
        return self._send(manager, now)

    def _offer(self, event):
        event = clean_event(event)
        if event is None:
            self.invalid += 1
            return
        if len(self.queue) >= MAX_EVENTS:
            self.queue.popleft()
            self.dropped += 1
        self.queue.append(event)

    def _send(self, manager, now):
        flight = self.flight
        if flight['retryAt'] is not None:
            if now < flight['retryAt']:
                return None
        elif flight['sentAt'] is not None and now - flight['sentAt'] < ACK_TIMEOUT:
            return None
        if flight['attempts'] >= MAX_ATTEMPTS:
            self._abandon(manager, 'undelivered')
            return None
        flight['attempts'] += 1
        again = flight['sent']
        try:
            self._request(manager, flight)
        except RuntimeError:
            # File réseau pleine, trop de requêtes en attente ou lien perdu : même UUID au tick suivant.
            self.send_errors += 1
            self.code = 'send_failed'
            return None
        except ValueError:
            self._abandon(manager, 'send_refused')
            return None
        if again:
            self.retries += 1
        flight['sent'], flight['sentAt'], flight['retryAt'] = True, now, None
        return REQUEST

    @staticmethod
    def _request(manager, flight):
        if flight['sent']:
            try:
                manager.retry(flight['id'])
                return
            except ValueError:
                # Le gestionnaire ne l'attend plus (refus déjà reçu) : même UUID, même charge, envoi direct.
                pass
        manager.request(REQUEST, json.loads(flight['body']), flight['id'])

    def _count(self, code):
        if code not in self.refused and len(self.refused) >= MAX_CODES:
            code = 'other'
        self.code = code
        self.refused[code] = self.refused.get(code, 0) + 1

    def _abandon(self, manager, code):
        flight, self.flight = self.flight, None
        self.dropped += 1
        self._count(code)
        manager.forget(flight['id'])

    def _purge(self):
        self.dropped += len(self.queue)
        self.queue.clear()

    def on_messages(self, manager, messages):
        for message in messages:
            if type(message) is not dict:
                continue
            flight = self.flight
            if flight is not None and flight['sent'] and message.get('id') == flight['id']:
                self._reply(manager, flight, message)
            elif message.get('id') is None and message.get('type') == REQUEST:
                self._receive(manager, message.get('payload'))

    def _reply(self, manager, flight, message):
        if message.get('ok') is True:
            self.flight = None
            # Accepté par le serveur, donc jamais renvoyé ; compté publié seulement si l'ACK reprend la requête.
            if self._acknowledged(manager, flight, message.get('payload')):
                self.published += 1
            else:
                self.ack_invalid += 1
                self._count('ack_invalid')
            return
        code = message.get('code')
        code = code if _text(code, _CODE) else 'invalid_code'
        if code in ('rate_limited', 'interaction_rate_limited') and flight['rate'] < MAX_RATE_RETRIES:
            flight['rate'] += 1
            flight['attempts'] = 0
            flight['retryAt'] = self.clock() + RATE_RETRY
            self.rate_limited += 1
            self.code = code
            return
        self.flight = None
        self.dropped += 1
        self._count(code)
        if code == 'unknown_type':
            # Serveur sans game.interaction : plus aucun envoi sur ce flux, ni nouveau join, ni nouveau flux.
            self.unsupported = True
            self._purge()
        elif code in STREAM_CODES:
            self.hold_until = self.clock() + HOLD
            self._purge()

    def _own(self, manager):
        identity = getattr(manager, 'identity', None)
        return identity.get('playerId') if isinstance(identity, dict) else None

    def _acknowledged(self, manager, flight, payload):
        observation = payload.get('observation') if type(payload) is dict else None
        if type(observation) is not dict:
            return False
        sent, own = json.loads(flight['body']), self._own(manager)
        if (observation.get('streamId') != sent['streamId'] or observation.get('event') != sent['event']
                or not _revision(observation.get('revision')) or observation.get('characterId') != self.flow[0]
                or observation.get('zoneId') != self.flow[1] or (own is not None and observation.get('playerId') != own)):
            return False
        self.revision = observation['revision']
        return True

    def _receive(self, manager, payload):
        if self.flow is None:
            self.ignored += 1
            return
        observation = clean_observation(payload, self._own(manager), self.flow[1], allow_own=True)
        if observation is None:
            self.rejected += 1
            return
        if observation['playerId'] == self._own(manager):
            self.ignored += 1  # L'écho serveur de notre observation n'est pas un flux distant invalide.
            return
        peers = self._confirmed_peers()
        if peers.get(observation['playerId']) != observation['streamId']:
            self.rejected += 1
            return
        sequence_key = (observation['playerId'], observation['streamId'], observation['event']['streamId'])
        # La révision appartient à une interaction ; la séquence appartient au flux observateur.
        key = sequence_key + (observation['event']['interactionId'],)
        if any(k[:2] == sequence_key[:2] and k[2] != sequence_key[2] for k in self.sequences):
            self.rejected += 1
            return
        if observation['event']['seq'] <= self.sequences.get(sequence_key, 0):
            self.duplicates += 1
            return
        if observation['revision'] <= self.seen.get(key, 0):
            self.duplicates += 1
            return
        self.seen[key] = observation['revision']
        self.sequences[sequence_key] = observation['event']['seq']
        self.sequences.move_to_end(sequence_key)
        self.seen.move_to_end(key)
        while len(self.seen) > MAX_STREAMS:
            self.seen.popitem(last=False)
        while len(self.sequences) > MAX_STREAMS:
            self.sequences.popitem(last=False)
        self.received.append(observation)
        self.received_count += 1

    def _confirmed_peers(self):
        """Seule la vue native confirmée autorise un flux reçu ; absence/erreur = aucune confiance."""
        try:
            peers = self.peer_streams() if self.peer_streams is not None else {}
        except Exception:
            peers = {}
        peers = peers if type(peers) is dict else {}
        for entries in (self.seen, self.sequences):
            for key in list(entries):
                if peers.get(key[0]) != key[1]:
                    del entries[key]
        self.received = deque((item for item in self.received if peers.get(item['playerId']) == item['streamId']), maxlen=RECEIVED)
        return peers

    def _state(self):
        if self.flow is None:
            return 'idle'
        if self.unsupported:
            return 'unsupported'
        if self.hold_until is not None:
            return 'held'
        if self.flight is None:
            return 'ready'
        return 'waiting' if self.flight['retryAt'] is not None else 'sending'

    def status(self):
        """État pour le diagnostic. `replay` : rien de reçu n'est appliqué au jeu."""
        return {'state': self._state(), 'queued': len(self.queue), 'inFlight': self.flight is not None,
                'published': self.published, 'dropped': self.dropped, 'invalid': self.invalid, 'retries': self.retries,
                'sendErrors': self.send_errors, 'rateLimited': self.rate_limited, 'ackInvalid': self.ack_invalid,
                'refused': dict(self.refused), 'lastCode': self.code, 'lastRevision': self.revision,
                'received': self.received_count, 'receivedRejected': self.rejected,
                'receivedDuplicates': self.duplicates, 'receivedIgnored': self.ignored,
                'recentReceived': [{'playerId': item['playerId'], 'streamId': item['streamId'],
                                    'revision': item['revision'], 'event': dict(item['event'])}
                                   for item in self.received],
                'replay': 'not_implemented', 'gameReadiness': 'NOT_TESTED_GAME'}
