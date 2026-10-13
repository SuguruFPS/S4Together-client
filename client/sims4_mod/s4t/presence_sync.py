"""Présence native automatique (#156) : game.join, game.state, game.leave, sans bouton ni commande.

Contrat : shared/protocol/EXTENSIONS.md « Présence native automatique ». Le mod déclare où se trouve le foyer
lié et publie les poses réellement lues ; il ne reçoit qu'en lecture seule. Aucun Sim distant n'est créé,
déplacé ou affiché : aucun rendu ni spawn n'est développé. Aucun besoin, humeur ou argent ne transite ici.

PresenceSync ne lit jamais le jeu directement : `read` et `poses` sont fournis par GameAdapter.
"""
import math
import re
import time
import uuid

from .journal import log
from .notify import game_notify

TITLE = 'S4Together'
STATE_SECONDS = 1.0
ACK_TICKS = 15
RETRY_TICKS = 30
MAX_POSES = 8
MAX_COORDINATE = 1000000.0
MIN_LEVEL, MAX_LEVEL = -8, 16  # Bornes serveur de l'étage.
_NATIVE = re.compile(r'^[0-9]{1,20}$')
_CODE = re.compile(r'^[a-z_]{1,40}$')
# Refus passagers : nouvel essai espacé de RETRY_TICKS, jamais à chaque seconde.
_TRANSIENT = ('storage_unavailable', 'request_limit', 'revision_conflict', 'error', 'ack_timeout', 'home_not_registered')
_ALERTS = {
    'lot_taken': 'Cette maison est déjà réservée par un autre foyer du serveur. Choisis une maison libre dans le jeu.',
    'members_changed': 'Le foyer a changé depuis la liaison du personnage (naissance, emménagement, départ). '
                       'Présence en ligne suspendue : une nouvelle liaison est nécessaire.',
    'binding_mismatch': 'Le serveur ne reconnaît pas ce foyer pour ce personnage. Présence en ligne suspendue.',
}


def _native(value):
    return isinstance(value, str) and _NATIVE.match(value) is not None


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def clean_poses(poses, sim_ids):
    """Poses publiables : Sims du binding, uniques, valeurs finies et bornées, 8 au plus. Le reste est écarté."""
    result, seen = [], set()
    for pose in poses if isinstance(poses, list) else []:
        if not isinstance(pose, dict) or pose.get('simId') not in sim_ids or pose['simId'] in seen:
            continue
        position = pose.get('position')
        if not isinstance(position, dict) or not _number(pose.get('rotation')):
            continue
        values = [position.get(axis) for axis in ('x', 'y', 'z')]
        if not all(_number(v) and abs(v) <= MAX_COORDINATE for v in values):
            continue
        value = {'simId': pose['simId'], 'position': dict(zip(('x', 'y', 'z'), values)), 'rotation': pose['rotation']}
        animation = pose.get('animation')
        if isinstance(animation, str) and 1 <= len(animation) <= 80:
            value['animation'] = animation  # Seulement si un nom réel a été observé ; jamais de valeur par défaut.
        level = pose.get('level')
        if type(level) is int and MIN_LEVEL <= level <= MAX_LEVEL:
            value['level'] = level  # Étage réellement lu ; absent plutôt que supposé.
        seen.add(pose['simId'])
        result.append(value)
        if len(result) == MAX_POSES:
            break
    return result


class PresenceSync:
    """Machine à états appelée par AutoLink chaque seconde, après CharacterSync. Jamais d'exception vers le jeu."""

    def __init__(self, read, poses, new_id=lambda: str(uuid.uuid4()), notify=game_notify, clock=time.monotonic,
                 remote=None):
        self.read = read
        # RemoteMotion : reçoit la vue de la zone jointe et ses événements ; PresenceSync n'applique rien lui-même.
        self.remote = remote
        self.poses = poses
        self.new_id = new_id
        self.notify = notify
        self.clock = clock
        self.alerted = set()  # Alertes déjà affichées dans cette session de jeu.
        self.leaving = []
        self.waiting = None
        # Vrai dès qu'un game.join est parti : le serveur peut porter une présence même sans ACK reçu.
        self.declared = False
        self.leave_failed = False
        self._clear()

    def _clear(self):
        self.state = 'idle'
        self.reason = None
        self.key = None
        self.joined = None
        self.zone_id = None
        self.blocked_key = None
        self.retry_in = None
        self.attempt = None
        self.stream_id = None
        self.sent_poses = None
        # Cadence game.state : `last_state` = dernier envoi, `anchor` = dernière réponse reçue (join accepté,
        # state accepté, state_rate_limited). Le serveur compte entre ses réceptions : un délai pris depuis
        # l'envoi ne les sépare pas d'une seconde quand la latence varie ; depuis la réponse, si.
        self.last_state = None
        self.anchor = None
        self.rate_limited = 0
        self.pause = 0
        self.published = 0
        self.rate_logged = False
        self.participants = set()
        self.remote_states = 0
        self.pending_events = []
        self.pending_overflow = False
        self.source = None
        self.unavailable = []

    # -- Boucle ---------------------------------------------------------------------------------------

    def tick(self, manager, authenticated, loaded, character):
        """`character` : personnage serveur `bound` vu par CharacterSync, sinon None."""
        if not authenticated:
            self.offline(manager)
            return None
        if not loaded:
            return self.leave(manager, 'unloaded')
        if character is None:
            if self.waiting is not None:
                self._age(manager)
            elif self.state == 'blocked':
                # Personnage redemandé au serveur (voyage) ou non lié : un ancien refus ne survit pas à la liaison.
                self._clear()
            return None
        try:
            identity = self.read()
        except Exception as error:
            left = self.leave(manager, 'native_read')
            self._block('native_read_' + type(error).__name__, None)
            return left
        key = self._key(identity)
        reason = self._check(identity, character)
        if reason is not None:
            left = self.leave(manager, reason)
            self._block(reason, key)
            return left
        if self.waiting is not None:
            if key == self.attempt[0]:
                self._age(manager)
                return None
            # Zone, foyer ou Sim actif changés pendant l'attente : la requête périmée est abandonnée tout de suite.
            manager.forget(self.waiting[0])
            self.waiting = None
            log('presence', 'context_changed')
        if self.state == 'blocked':
            if key == self.blocked_key and not self._retry_due():
                # Un leave resté sans envoi (file pleine, lien coupé) est retenté sans lever le blocage.
                return self._send_leave(manager, self.reason) if self.declared else None
            self.state = 'idle'
        if self.state == 'joined' and key == self.key:
            return self._publish(manager)
        return self._join(manager, identity, key, character)

    def _age(self, manager):
        self.waiting[2] += 1
        if self.waiting[2] >= ACK_TICKS:
            self._timeout(manager)

    def _retry_due(self):
        if self.retry_in is None:
            return False
        self.retry_in -= 1
        return self.retry_in <= 0

    @staticmethod
    def _key(identity):
        if not isinstance(identity, dict):
            return None
        sims = identity.get('simIds')
        return (identity.get('saveId'), identity.get('nativeHouseholdId'), identity.get('activeSimId'),
                tuple(sims) if isinstance(sims, list) else None, identity.get('worldId'), identity.get('lotId'), identity.get('homeLotId'))

    def _check(self, identity, character):
        """Raison de ne pas déclarer la présence, ou None. Aucune identité n'est complétée ni devinée."""
        if not isinstance(identity, dict):
            return 'native_ids_unavailable'
        self.source = identity.get('saveIdSource')
        self.unavailable = list(identity.get('unavailable') or [])
        save, sims = identity.get('saveId'), identity.get('simIds')
        if (self.unavailable or not isinstance(save, str) or not 1 <= len(save) <= 64 or any(ord(c) < 32 or ord(c) == 127 for c in save)
                or not all(_native(identity.get(name)) for name in ('nativeHouseholdId', 'activeSimId', 'worldId', 'lotId'))
                or not isinstance(sims, list) or not 1 <= len(sims) <= MAX_POSES or len(set(sims)) != len(sims)
                or not all(_native(sim) for sim in sims)):
            return 'native_ids_unavailable'
        if identity.get('homeLotId') is not None and not _native(identity['homeLotId']):
            return 'native_ids_unavailable'
        binding = character.get('binding') if isinstance(character, dict) else None
        if not isinstance(binding, dict) or binding.get('gameHouseholdId') != identity['nativeHouseholdId']:
            return 'household_mismatch'
        bound = binding.get('simIds')
        pets = identity.get('petSimIds', [])
        valid_pets = (isinstance(pets, list) and all(_native(p) for p in pets)
                      and len(set(pets)) == len(pets) and set(pets).issubset(sims))
        if isinstance(bound, list) and (not set(bound).issubset(sims) or not valid_pets or not (set(sims) - set(bound)).issubset(pets)):
            # Naissance, emménagement, départ : pas de mise à jour silencieuse du binding.
            return 'members_changed'
        if identity['activeSimId'] not in sims or isinstance(bound, list) and identity['activeSimId'] not in bound:
            return 'active_sim_outside_household'
        return None

    def _block(self, reason, key, retry=False):
        first = self.state != 'blocked' or (reason, key) != (self.reason, self.blocked_key)
        self.state, self.reason, self.blocked_key = 'blocked', reason, key
        self.retry_in = RETRY_TICKS if retry else None
        if retry and reason == 'home_not_registered':
            self.retry_in = 2  # La réservation autonome peut être encore en vol au premier join.
        if not first:
            return
        # Une ligne par transition ; les identités natives ne sont jamais écrites dans le journal.
        log('presence', reason)
        if reason in _ALERTS and reason not in self.alerted:
            self.alerted.add(reason)
            self.notify(TITLE, _ALERTS[reason])

    # -- Requêtes -------------------------------------------------------------------------------------

    def _send(self, manager, kind, payload):
        request_id = self.new_id()
        manager.request(kind, payload, request_id)
        self.waiting = [request_id, kind, 0]
        return kind

    def _join(self, manager, identity, key, character):
        bound = character['binding'].get('simIds', identity['simIds'])
        members = [sim for sim in identity['simIds'] if sim in bound]
        payload = {'saveId': identity['saveId'], 'nativeHouseholdId': identity['nativeHouseholdId'],
                   'activeSimId': identity['activeSimId'], 'simIds': list(members),
                   'worldId': identity['worldId'], 'lotId': identity['lotId']}
        if 'homeLotId' in identity:
            payload['homeLotId'] = identity['homeLotId']
        self._send(manager, 'game.join', payload)
        self.pending_events = []
        self.pending_overflow = False
        self.declared = True
        self.attempt = (key, payload)
        if self.state != 'joined':
            self.state = 'joining'
        return 'game.join'

    def _publish(self, manager):
        if self.pause > 0:
            self.pause -= 1
            return None
        try:
            poses = clean_poses(self.poses(self.joined['simIds']), self.joined['simIds'])
        except Exception as error:
            log('presence_read', type(error).__name__)
            self.pause = RETRY_TICKS
            return None
        if not poses or poses == self.sent_poses:
            return None
        now = self.clock()
        if any(mark is not None and now - mark < STATE_SECONDS for mark in (self.anchor, self.last_state)):
            return None
        payload = {'poses': poses}
        if self.stream_id is not None:
            payload['streamId'] = self.stream_id  # Flux de ce join : le serveur refuse un contexte périmé.
        self._send(manager, 'game.state', payload)
        self.attempt = (self.key, poses)
        self.last_state = now
        return 'game.state'

    def active(self):
        """Vrai si le serveur peut porter une présence de ce joueur : join parti, confirmé ou non, sans leave."""
        return self.declared

    def leave(self, manager, reason):
        """Retire la présence possiblement publiée (déchargement, menu, blocage, ACK perdu).

        Sans effet si aucun game.join n'est parti. Si l'envoi échoue, la présence reste « à retirer » et le
        leave est retenté au prochain appel, tant que la connexion est disponible.
        """
        if not self.declared:
            return None
        if self.waiting is not None:
            try:
                manager.forget(self.waiting[0])
            except Exception as error:
                log('presence', 'forget_' + type(error).__name__)
            self.waiting = None
        if self.state != 'idle' or self.reason is not None:
            self._clear()
        return self._send_leave(manager, reason)

    def _send_leave(self, manager, reason):
        try:
            request_id = self.new_id()
            manager.request('game.leave', {}, request_id)
        except Exception as error:
            if not self.leave_failed:
                self.leave_failed = True
                log('presence', 'leave_' + type(error).__name__)
            return None
        self.declared = self.leave_failed = False
        log('presence', 'left:' + reason)
        self.leaving = (self.leaving + [request_id])[-8:]
        return 'game.leave'

    def offline(self, manager):
        """Lien perdu : tout sera redéclaré à la reconnexion. `declared` est gardé : si le launcher est resté
        connecté, le serveur porte encore la présence, et un retour hors partie devra envoyer game.leave."""
        stale = list(self.leaving) + ([self.waiting[0]] if self.waiting is not None else [])
        for request_id in stale:
            try:
                manager.forget(request_id)
            except Exception as error:
                log('presence', 'forget_' + type(error).__name__)
        was_joined = self.state == 'joined'
        self.leaving, self.waiting = [], None
        if self.state != 'idle' or self.reason is not None:
            self._clear()
        if was_joined:
            log('presence', 'offline')

    def _timeout(self, manager):
        request_id, kind, _ = self.waiting
        self.waiting = None
        manager.forget(request_id)
        log('presence', kind + ':ack_timeout')
        if kind == 'game.join':
            # ACK perdu : le join a pu être accepté. La présence est retirée avant le nouvel essai.
            key = self.attempt[0]
            self.leave(manager, 'ack_timeout')
            self._block('ack_timeout', key, retry=True)
        else:
            self.pause = RETRY_TICKS

    # -- Réponses et événements -------------------------------------------------------------------------

    def on_messages(self, manager, messages):
        for message in messages:
            if not isinstance(message, dict):
                continue
            request_id = message.get('id')
            if request_id is None:
                if message.get('type') in ('game.presence', 'game.state'):
                    if self.waiting is not None and self.waiting[1] == 'game.join':
                        # Le serveur diffuse avant l'ACK : conserver les transitions reçues pendant ce join.
                        self.pending_events.append(message)
                        if len(self.pending_events) > 64:
                            self.pending_events.pop(0)
                            self.pending_overflow = True
                    else:
                        self._event(message)
            elif request_id in self.leaving:
                self.leaving.remove(request_id)
            elif self.waiting is not None and request_id == self.waiting[0]:
                kind = self.waiting[1]
                self.waiting = None
                payload = message.get('payload') if isinstance(message.get('payload'), dict) else {}
                code = message.get('code')
                # Seul un code de forme connue atteint le journal ; jamais un texte libre du serveur.
                code = code if isinstance(code, str) and _CODE.match(code) else 'error'
                if kind == 'game.join':
                    self._joined(manager, payload) if message.get('ok') is True else self._join_refused(manager, code)
                elif message.get('ok') is True:
                    self.anchor = self.clock()
                    self.sent_poses = self.attempt[1]
                    self.published += 1
                else:
                    self._state_refused(manager, code)

    def _joined(self, manager, payload):
        key, sent = self.attempt
        presence = payload.get('presence') if isinstance(payload.get('presence'), dict) else {}
        expected = 'native:{}:{}'.format(sent['worldId'], sent['lotId'])
        if presence.get('zoneId') != expected:
            # Join accepté mais réponse incohérente : la présence publiée est retirée.
            self.leave(manager, 'join_invalid_ack')
            self._block('join_invalid_ack', key)
            return
        source, unavailable = self.source, self.unavailable
        pending, overflow = self.pending_events, self.pending_overflow
        self._clear()
        self.source, self.unavailable = source, unavailable
        self.state, self.key, self.joined, self.zone_id = 'joined', key, sent, expected
        stream = payload.get('streamId', presence.get('streamId'))
        self.stream_id = stream if isinstance(stream, str) and 1 <= len(stream) <= 64 else None
        self.anchor = self.clock()
        self._view(payload)
        scene = dict(payload)
        if overflow:
            # Un ancien snapshot n'est pas appliqué si des transitions ont été perdues ; RemoteMotion redemande.
            scene.pop('presences', None)
            scene.pop('sims', None)
        self._relay('on_join', expected, scene)
        for message in pending:
            self._event(message)
        log('presence', 'joined')

    def _join_refused(self, manager, code):
        key = self.attempt[0]
        if self.joined is not None:
            # Nouvelle zone refusée : l'ancienne présence confirmée ne doit pas rester publiée.
            self.leave(manager, code)
        else:
            self.declared = False  # Refus explicite d'un premier join : le serveur n'a rien publié.
        self.joined = self.key = self.zone_id = None
        self._block(code, key, retry=code in _TRANSIENT)

    def _state_refused(self, manager, code):
        if code == 'state_rate_limited':
            # Cadence serveur : la même pose repartira une seconde après ce refus, sans alerte.
            self.anchor = self.clock()
            self.rate_limited += 1
            if not self.rate_logged:
                self.rate_logged = True
                log('presence', 'state_rate_limited')
            return
        if code == 'stale_stream':
            # Flux remplacé côté serveur (voyage, autre join) : une présence existe encore, on rejoint.
            log('presence', 'stale_stream')
            self._clear()
            return
        if code == 'game_join_required':
            log('presence', 'rejoin')
            self._clear()
            self.declared = False  # Le serveur déclare lui-même ne porter aucune présence.
            return
        if code in _TRANSIENT:
            log('presence', 'game.state:' + code)
            self.pause = RETRY_TICKS
            return
        key = self.key
        self.leave(manager, code)
        self._block(code, key)

    def _view(self, payload):
        participants = payload.get('participants')
        if isinstance(participants, list) and len(participants) <= 4096:
            self.participants = {p for p in participants if isinstance(p, str)}

    def _relay(self, name, *args):
        if self.remote is None:
            return
        try:
            getattr(self.remote, name)(*args)
        except Exception as error:
            log('presence', 'remote_' + type(error).__name__)

    def _event(self, message):
        """Vue de la zone pour le diagnostic ; les poses sont relayées à RemoteMotion, seul à toucher aux clones."""
        payload = message.get('payload')
        if not isinstance(payload, dict) or self.zone_id is None:
            return
        if message['type'] == 'game.state':
            if payload.get('zoneId') == self.zone_id:
                self.remote_states += 1
            self._relay('on_event', 'state', payload)
            return
        self._relay('on_event', 'presence', payload)
        presence = payload.get('presence') if isinstance(payload.get('presence'), dict) else {}
        if presence.get('zoneId') == self.zone_id and presence.get('online') is not False:
            self._view(payload)
        elif payload.get('previousZoneId') == self.zone_id or presence.get('zoneId') == self.zone_id:
            self.participants.discard(presence.get('playerId'))

    def status(self):
        """État pour s4t.diagnostic : ni identité de joueur, ni jeton, ni position."""
        return {'state': self.state, 'reason': self.reason, 'zoneId': self.zone_id, 'saveIdSource': self.source,
                'unavailable': list(self.unavailable), 'sims': len(self.joined['simIds']) if self.joined else 0,
                'statesAcknowledged': self.published, 'statesRateLimited': self.rate_limited,
                'participants': len(self.participants), 'remoteStatesSeen': self.remote_states,
                'remoteRendering': 'not_implemented' if self.remote is None else 'remote_motion',
                'gameReadiness': 'NOT_TESTED_GAME'}
