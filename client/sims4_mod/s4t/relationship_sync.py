"""Relations communes (#159, 20/10/2026) : amitié et romance entre le Sim de ce joueur et la copie d'un autre joueur
sont les mêmes des deux côtés. Le serveur garde une valeur par paire de personnages (`relationship.set`, révision CAS) ;
chaque partie applique la valeur commune puis publie ses propres changements.

Fusion : quand une valeur commune plus récente arrive, l'écart local accumulé depuis la dernière valeur commune est
conservé et republié (canonique + (local - base)), pour que deux joueurs qui interagissent en même temps ne s'écrasent pas.

API vérifiée par désassemblage de simulation.zip (Python 3.7) :
RelationshipTracker.get_relationship_score(target_sim_id, track) (délègue à services.relationship_service()) ;
RelationshipTracker.set_relationship_score(target_sim_id, value, track, threshold=…) ;
relationships.relationship_track.RelationshipTrack.FRIENDSHIP_TRACK / ROMANCE_TRACK ; sim_info.relationship_tracker.
"""
import time
import uuid

from .journal import log
from .remote_household import parse_description

MIN_VALUE = -100
MAX_VALUE = 100
# Contrôle des scores locaux ; un même couple n'est pas republié plus souvent que RATE_SECONDS.
POLL_SECONDS = 5.0
RATE_SECONDS = 10.0
ACK_SECONDS = 15.0
# Écart minimal publié : la lente décroissance des scores ne produit pas d'envoi à chaque contrôle.
MIN_DELTA = 2


def clamp(value):
    return max(MIN_VALUE, min(MAX_VALUE, int(round(value))))


def _whole(value):
    return isinstance(value, int) and not isinstance(value, bool)


def valid_view(view):
    if not isinstance(view, dict):
        return False
    texts = ('characterA', 'characterB', 'ownerA', 'ownerB')
    if any(not isinstance(view.get(k), str) or not view.get(k) for k in texts):
        return False
    if not all(_whole(view.get(k)) for k in ('friendship', 'romance', 'revision')) or view['revision'] < 1:
        return False
    return MIN_VALUE <= view['friendship'] <= MAX_VALUE and MIN_VALUE <= view['romance'] <= MAX_VALUE


def pair_key(view):
    return view['characterA'] + '|' + view['characterB']


def other_side(view, character_id):
    """(propriétaire, personnage, actuellement choisi) de l'autre côté, ou None si character_id n'est pas dans la paire."""
    if view['characterA'] == character_id:
        return view['ownerB'], view['characterB'], view.get('currentB') is True
    if view['characterB'] == character_id:
        return view['ownerA'], view['characterA'], view.get('currentA') is True
    return None


def _scores(view):
    return view['friendship'], view['romance']


class RelationshipSync:
    def __init__(self, game=None, clock=time.monotonic, new_id=None):
        self.game = game if game is not None else GameRelationships()
        self.clock = clock
        self.new_id = new_id or (lambda: str(uuid.uuid4()))
        self.canonical = {}
        self.disabled = False
        self.reset()

    def reset(self):
        self.character_id = None
        # joueur cible -> (clé de paire ou None, amitié, romance, révision commune connue)
        self.baseline = {}
        self.waiting = None
        self.waiting_at = 0.0
        self.next_check = 0.0
        self.last_sent = {}

    def merge(self, view):
        if not valid_view(view):
            return False
        key = pair_key(view)
        known = self.canonical.get(key)
        if known is not None and known['revision'] > view['revision']:
            return False
        if known is not None and known['revision'] == view['revision'] and known == view:
            return False
        self.canonical[key] = dict(view)
        return True

    def on_messages(self, manager, messages):
        for message in messages:
            if not isinstance(message, dict):
                continue
            payload = message.get('payload') if isinstance(message.get('payload'), dict) else {}
            if message.get('type') == 'relationship':
                self.merge(payload.get('relationship'))
            elif message.get('type') == 'snapshot':
                self._merge_snapshot(payload)
            elif self.waiting is not None and message.get('id') == self.waiting[0]:
                self._reply(manager, message, payload)

    def _reply(self, manager, message, payload):
        target = self.waiting[1]
        self.waiting = None
        if message.get('ok') is True:
            view = payload.get('relationship')
            if self.merge(view) or valid_view(view):
                # Notre propre écriture devient la base : elle ne doit pas être refusionnée comme un changement distant.
                self.baseline[target] = (pair_key(view), view['friendship'], view['romance'], view['revision'])
            return
        code = message.get('code') if isinstance(message.get('code'), str) else 'error'
        log('relationship_sync', 'set:' + code)
        if code == 'unknown_type':
            # Serveur sans relations communes (avant 0.1.48) : aucune nouvelle tentative.
            self.disabled = True
        elif code == 'stale_revision':
            # La valeur commune plus récente arrive par événement ; le snapshot rattrape un événement manqué.
            manager.request('snapshot', {}, self.new_id())
        else:
            # Refus durable (personnage absent, limite) : on reprend la valeur locale comme base sans republier.
            self.baseline.pop(target, None)
            self.last_sent[target] = self.clock() + 60.0

    def _merge_snapshot(self, snapshot):
        views = snapshot.get('relationships') if isinstance(snapshot, dict) else None
        if isinstance(views, list):
            for view in views:
                self.merge(view)

    def tick(self, manager, character):
        if self.disabled:
            return None
        character_id = character.get('characterId') if isinstance(character, dict) else None
        if not isinstance(character_id, str) or not character_id:
            if self.character_id is not None:
                self.reset()
            return None
        if character_id != self.character_id:
            self.reset()
            self.character_id = character_id
        now = self.clock()
        if self.waiting is not None:
            if now - self.waiting_at >= ACK_SECONDS:
                manager.request('relationship.set', dict(self.waiting[2]), self.waiting[0])
                self.waiting_at = now
            return None
        if now < self.next_check:
            return None
        self.next_check = now + POLL_SECONDS
        self._merge_snapshot(getattr(manager, 'snapshot', None))
        own = self.game.own(character)
        if own is None:
            return None
        for player_id, ghost in sorted(self.game.ghosts().items()):
            sent = self._pair(manager, own, player_id, ghost, now)
            if sent:
                return sent
        return None

    def _view_for(self, player_id):
        for view in self.canonical.values():
            side = other_side(view, self.character_id)
            if side is not None and side[0] == player_id and side[2]:
                return view
        return None

    def _pair(self, manager, own, player_id, ghost, now):
        local = tuple(clamp(v) for v in self.game.read(own, ghost))
        view = self._view_for(player_id)
        base = self.baseline.get(player_id)
        if view is not None and (base is None or base[0] != pair_key(view) or view['revision'] > base[3]):
            canon = _scores(view)
            if base is not None and base[0] == pair_key(view):
                wanted = tuple(clamp(c + (l - b)) for c, l, b in zip(canon, local, base[1:3]))
            else:
                wanted = canon
            if local != canon:
                self.game.write(own, ghost, canon)
            self.baseline[player_id] = (pair_key(view), canon[0], canon[1], view['revision'])
            log('relationship_sync', 'applied')
            if wanted != canon:
                return self._send(manager, player_id, wanted, view['revision'], now)
            return None
        if view is None and (base is None or base[0] is not None):
            # Pas encore de valeur commune avec ce personnage : la valeur locale sert de base, publiée si non nulle.
            self.baseline[player_id] = (None, 0, 0, 0)
            base = self.baseline[player_id]
        if max(abs(l - b) for l, b in zip(local, base[1:3])) < MIN_DELTA:
            return None
        if now - self.last_sent.get(player_id, -RATE_SECONDS) < RATE_SECONDS:
            return None
        return self._send(manager, player_id, local, base[3], now)

    def _send(self, manager, player_id, scores, revision, now):
        payload = {'targetPlayerId': player_id, 'expectedRevision': revision,
                   'friendship': scores[0], 'romance': scores[1]}
        request_id = self.new_id()
        manager.request('relationship.set', payload, request_id)
        self.waiting = (request_id, player_id, payload)
        self.waiting_at = now
        self.last_sent[player_id] = now
        return 'relationship.set'


class GameRelationships:
    """Accès au jeu : copies des autres joueurs (foyers S4T), Sim lié du joueur, scores amitié/romance."""

    def ghosts(self):
        import services
        found = {}
        for household in tuple(services.household_manager().values()):
            parsed = parse_description(getattr(household, 'description', None))
            if parsed is None or parsed[0] in found:
                continue
            sim_infos = list(household.sim_info_gen())
            if len(sim_infos) == 1:
                found[parsed[0]] = sim_infos[0]
        return found

    def own(self, character):
        import services
        binding = character.get('binding') if isinstance(character.get('binding'), dict) else {}
        ids = [str(x) for x in binding.get('simIds') or ()]
        active = services.active_sim_info()
        if active is not None and str(active.sim_id) in ids:
            return active
        manager = services.sim_info_manager()
        for sim_id in ids:
            sim_info = manager.get(int(sim_id))
            if sim_info is not None:
                return sim_info
        return None

    @staticmethod
    def _tracks():
        from relationships.relationship_track import RelationshipTrack
        return RelationshipTrack.FRIENDSHIP_TRACK, RelationshipTrack.ROMANCE_TRACK

    def read(self, own, ghost):
        tracker = own.relationship_tracker
        return tuple(tracker.get_relationship_score(ghost.sim_id, track) or 0 for track in self._tracks())

    def write(self, own, ghost, scores):
        tracker = own.relationship_tracker
        for track, value in zip(self._tracks(), scores):
            tracker.set_relationship_score(ghost.sim_id, value, track)
