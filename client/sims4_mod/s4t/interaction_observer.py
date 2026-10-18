"""Observation des interactions du Sim actif : relevé périodique sur le thread principal, lecture seule.

Rien n'est appliqué au jeu : aucun rappel n'est enregistré sur une interaction, aucune interaction n'est créée,
annulée ou poussée. Les mixers et la file d'attente ne sont pas observés. L'envoi au serveur est fait ailleurs
(interaction_publisher), jamais ici.

API vérifiée par désassemblage (1.128.90.1030, lecture seule), jamais exécutée en jeu — NOT_TESTED_GAME :
simulation.zip — services.get_active_sim(), services.active_household_id() ; Sim.si_state (SIState, itérable
sur _super_interactions), Sim.queue (InteractionQueue.running), Sim.sim_info, Sim.is_sim ;
Interaction.id (unique_id : compteur du processus, ni persistant ni commun à deux machines),
Interaction.affordance.guid64 (instance de tuning), .is_super, .pipeline_progress (PipelineProgress 0..6),
.target, .visible, .source, .is_user_directed, .is_autonomous, .is_finishing, .is_finishing_naturally,
.has_been_canceled, .has_been_user_canceled, .has_been_reset, .transition_failed, .finishing_type
(FinishingType) ; BaseObject.is_sim / .is_part / .is_terrain / .definition, Definition.id, Part.part_owner ;
SimInfo.sim_id, .household_id, .household ; Household.description. Détail : docs/NATIVE_INTERACTIONS.md.

Un clone distant n'est nommé que si son foyer figure dans la carte validée par l'apparence reçue
(`validated_clones`) et que ce Sim précis est relié au Sim natif du joueur distant (`validated_remote_sims`).
La description d'un foyer ne suffit jamais : elle est modifiable dans la partie.
"""
import re
import math
import threading
import time
import uuid
import weakref
from collections import deque

from .remote_household import PREFIX, parse_description

MAX_READ = 32
MAX_TRACKED = 32
MAX_EVENTS = 64
RECENT = 8
MIN_INTERVAL = 0.5
MAX_ELAPSED_MS = 10 ** 9
MAX_SEQ = 2 ** 53
KIND, VERSION = 's4t.interaction', 1

# interactions.PipelineProgress : NONE=0 et QUEUED=1 ne sont pas observés.
PROGRESS = {2: 'preparing', 3: 'preparing', 4: 'running', 5: 'running', 6: 'exited'}
_RANK = {'preparing': 1, 'running': 2}
ENDINGS = ('finished', 'user_canceled', 'canceled', 'failed', 'reset', 'unknown', 'unobserved')
STATES = tuple(_RANK) + ENDINGS
ORIGINS = ('user', 'autonomy', 'script', 'unknown')
# interactions.interaction_finisher.FinishingType, noms en minuscules. Tout autre nom est écarté.
FINISHING = frozenset((
    'killed', 'auto_exit', 'displaced', 'natural', 'reset', 'user_cancel', 'si_finished', 'target_deleted',
    'failed_tests', 'transition_failure', 'interaction_incompatibility', 'interaction_queue', 'priority', 'socials',
    'wait_in_line', 'object_changed', 'situations', 'crafting', 'liability', 'dialog', 'conditional_exit', 'fire',
    'wedding', 'routing_formation', 'unknown'))

# Toujours fullmatch() : `$` avec match() accepte un saut de ligne final. Classes ASCII explicites, jamais `\d`.
_DECIMAL = re.compile(r'[1-9][0-9]{0,19}')
_PLAYER = re.compile(r'[\x21-\x7e]{1,64}')
_STREAM = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}')
_SHA16 = re.compile(r'[0-9a-f]{16}')
_SHA256 = re.compile(r'[0-9a-f]{64}')


def _text(value, pattern):
    """Chaîne exacte (pas une sous-classe) reconnue en entier : ni saut de ligne final ni caractère de contrôle."""
    return type(value) is str and pattern.fullmatch(value) is not None


def _decimal(value):
    return _text(value, _DECIMAL)


def _player(value):
    return _text(value, _PLAYER)


def _word(value, allowed):
    return type(value) is str and value in allowed


def _number(value):
    """Entier natif strictement positif en chaîne décimale ; booléen, flottant et zéro refusés."""
    if isinstance(value, int) and not isinstance(value, bool) and 0 < value < 10 ** 20:
        return str(int(value))
    return None


_TARGETS = {'none': (), 'self': (), 'terrain': (), 'local_sim': (), 'unknown': (),
            'household_sim': (('simId', _decimal),),
            # simId : Sim natif du joueur distant (celui de son jeu), jamais l'identifiant local du clone.
            'remote_clone': (('playerId', _player), ('simId', _decimal)),
            'object': (('definitionId', _decimal), ('part', lambda value: type(value) is bool))}
_REQUIRED = frozenset(('kind', 'v', 'streamId', 'seq', 'interactionId', 'actorSimId', 'affordanceId', 'state', 'origin',
                       'visible', 'target'))
_ENDING_ONLY = frozenset(('finishingType', 'elapsedMs'))


def clean_target(value):
    """Cible strictement typée, ou None. Aucun nom, aucun identifiant d'objet ou de clone local à la partie."""
    if type(value) is not dict or type(value.get('kind')) is not str:
        return None
    fields = _TARGETS.get(value['kind'])
    placement = value.get('placement')
    expected = {'kind'} | {name for name, _ in fields} if fields is not None else set()
    if value['kind'] == 'object' and 'placement' in value:
        expected.add('placement')
        if (type(placement) is not dict or set(placement) != {'x', 'y', 'z', 'level'}
                or any(type(placement[k]) is not int or abs(placement[k]) > 10 ** 9 for k in ('x', 'y', 'z'))
                or type(placement['level']) is not int or not -8 <= placement['level'] <= 16):
            return None
    if fields is None or set(value) != expected:
        return None
    if not all(valid(value[name]) for name, valid in fields):
        return None
    result = dict(value)
    if placement is not None:
        result['placement'] = dict(placement)
    return result


def clean_event(value):
    """Événement conforme au contrat v1, ou None. Tout champ inconnu fait refuser l'événement entier."""
    if type(value) is not dict:
        return None
    keys = set(value)
    if not _REQUIRED <= keys or not keys <= _REQUIRED | _ENDING_ONLY:
        return None
    state, seq = value['state'], value['seq']
    target = clean_target(value['target'])
    if (type(value['kind']) is not str or value['kind'] != KIND or type(value['v']) is not int
            or value['v'] != VERSION or target is None or not _text(value['streamId'], _STREAM)
            or type(seq) is not int or not 0 < seq < MAX_SEQ
            or not all(_decimal(value[name]) for name in ('interactionId', 'actorSimId', 'affordanceId'))
            or not _word(state, STATES) or not _word(value['origin'], ORIGINS) or type(value['visible']) is not bool):
        return None
    if keys & _ENDING_ONLY and state not in ENDINGS:
        return None
    if 'finishingType' in value and not _word(value['finishingType'], FINISHING):
        return None
    if 'elapsedMs' in value and (type(value['elapsedMs']) is not int or not 0 <= value['elapsedMs'] < MAX_ELAPSED_MS):
        return None
    result = dict(value)
    result['target'] = target
    return result


def validated_clones(ghosts, appearances):
    """{foyer local: joueur} pour les seuls clones dont l'empreinte est celle de l'apparence reçue et validée.

    `ghosts` : RemoteHouseholds.ghosts ; `appearances` : AppearanceSync.remote. Un clone en attente
    (`pending-not-done`), une empreinte non hexadécimale ou un foyer revendiqué par deux joueurs ne donnent rien.
    La carte prouve le foyer du clone, pas un Sim précis de ce foyer.
    """
    if type(ghosts) is not dict or type(appearances) is not dict:
        return {}
    found, twice = {}, set()
    for player, ghost in ghosts.items():
        seen = appearances.get(player)
        if not _player(player) or type(ghost) is not dict or type(seen) is not dict:
            continue
        household, short, full = ghost.get('householdId'), ghost.get('sha16'), seen.get('sha256')
        if not _decimal(household) or not _text(short, _SHA16) or not _text(full, _SHA256) or full[:16] != short:
            continue
        if household in found:
            twice.add(household)
        found[household] = player
    return {household: player for household, player in found.items() if household not in twice}


def validated_remote_sims(ghosts, appearances, mapping):
    """{(foyer local, Sim local du clone): (joueur, Sim natif distant)}, correspondances cohérentes seulement.

    `mapping` : RemoteMotion.mapping, écrit après un Locate réussi : (joueur, Sim distant) -> (foyer local, Sim
    local). Une entrée n'est gardée que si le foyer est celui du clone validé de ce joueur (`validated_clones`) et
    si le Sim distant est celui de l'apparence reçue. Un Sim local relié deux fois ne donne rien.
    """
    clones = validated_clones(ghosts, appearances)
    if not clones or type(mapping) is not dict:
        return {}
    found, twice = {}, set()
    for key, value in mapping.items():
        if type(key) is not tuple or len(key) != 2 or type(value) is not tuple or len(value) != 2:
            continue
        (player, remote), (household, local) = key, value
        if not _player(player) or not all(_decimal(number) for number in (remote, household, local)):
            continue
        if clones.get(household) != player or appearances[player].get('simId') != remote:
            continue
        if value in found:
            twice.add(value)
        found[value] = key
    return {local: remote for local, remote in found.items() if local not in twice}


def _clean_entry(item):
    """Relevé d'une interaction rendu par le lecteur : mêmes règles que le contrat, plus l'objet natif."""
    if type(item) is not dict or item.get('native') is None:
        return None
    local, target = _number(item.get('localId')), clean_target(item.get('target'))
    progress, origin = item.get('progress'), item.get('origin')
    if (local is None or target is None or not _decimal(item.get('affordanceId'))
            or not _word(progress, tuple(PROGRESS.values())) or not _word(origin, ORIGINS)
            or type(item.get('visible')) is not bool):
        return None
    return {'native': item['native'], 'localId': local, 'affordanceId': item['affordanceId'], 'progress': progress,
            'origin': origin, 'visible': item['visible'], 'target': target}


class _Tracked:
    __slots__ = ('ref', 'actor', 'local', 'affordance', 'origin', 'visible', 'target', 'state', 'since', 'closed')

    def native(self):
        return self.ref() if self.ref is not None else None


class InteractionObserver:
    """Suit les super-interactions relevées et émet chaque état une seule fois, dans l'ordre.

    `reader.read()` rend None (rien de chargé) ou {'actorSimId', 'interactions', 'truncated', 'unidentified'} ;
    `reader.outcome(native)` rend (fin, finishingType ou None). Les objets natifs ne sont tenus que par
    référence faible : l'observateur ne prolonge la vie d'aucune interaction du jeu.
    """

    def __init__(self, reader, clock=time.monotonic, new_id=lambda: str(uuid.uuid4())):
        self.reader = reader
        self.clock = clock
        self.new_id = new_id
        self.tracked = {}
        self.pending = deque()
        self.recent = deque(maxlen=RECENT)
        self.dropped = 0
        self.error = None
        self.reset()

    def reset(self):
        """Zone chargée ou déchargée : les interactions suivies n'existent plus. Nouveau flux, aucune fin inventée."""
        self.tracked.clear()
        self.pending.clear()
        self.recent.clear()
        self.stream_id = self.new_id()
        self.seq = 0
        self.actor = None
        self.last = None
        self.state = 'idle'
        self.overflow = self.rejected = self.unidentified = 0
        self.truncated = False

    def tick(self):
        """Un relevé au plus par MIN_INTERVAL. Rend les événements nouveaux ; ne lève jamais."""
        now = self.clock()
        if self.last is not None and now - self.last < MIN_INTERVAL:
            return []
        self.last = now
        try:
            reading = self.reader.read()
        except Exception as error:
            self.state, self.error = 'read_error', type(error).__name__
            return []
        actor = reading.get('actorSimId') if isinstance(reading, dict) else None
        if not _decimal(actor):
            # Rien de chargé : ce qui est suivi le reste, sa fin sera constatée au prochain relevé réel.
            self.state = 'unavailable'
            return []
        self.state = 'observing'
        self.truncated = reading.get('truncated') is True
        unidentified = reading.get('unidentified')
        self.unidentified = unidentified if type(unidentified) is int and 0 <= unidentified <= 1024 else 0
        entries, self.rejected = self._entries(reading.get('interactions'))
        events = []
        if actor != self.actor:
            for record in self.tracked.values():
                self._end(record, ('unobserved', None), now, events)
            self.tracked.clear()
            self.actor = actor
        for local, record in list(self.tracked.items()):
            entry = entries.get(local)
            if entry is not None and (record.ref is None or record.native() is entry['native']):
                continue
            # Disparue, ou même identifiant local porté par un autre objet : l'ancienne est terminée.
            del self.tracked[local]
            self._end(record, self._outcome(record.native()), now, events)
        self.overflow = 0
        for local in sorted(entries, key=int):
            self._advance(actor, entries[local], now, events)
        return events

    @staticmethod
    def _entries(raw):
        entries, rejected = {}, 0
        for item in (raw if isinstance(raw, list) else [])[:MAX_READ * 2]:
            entry = _clean_entry(item)
            if entry is None or entry['localId'] in entries:
                rejected += 1
            else:
                entries[entry['localId']] = entry
        return entries, rejected

    def _advance(self, actor, entry, now, events):
        record, progress = self.tracked.get(entry['localId']), entry['progress']
        if record is None:
            if len(self.tracked) >= MAX_TRACKED:
                self.overflow += 1
                return
            record = self._track(actor, entry, now)
            if progress == 'exited':
                # Jamais vue en cours : ni début ni fin ne sont inventés.
                record.closed = True
                return
            record.state = progress
            self._emit(record, progress, events)
        elif record.closed:
            return
        elif progress == 'exited':
            self._end(record, self._outcome(entry['native']), now, events)
        elif _RANK[progress] > _RANK[record.state]:
            record.state = progress
            self._emit(record, progress, events)

    def _track(self, actor, entry, now):
        record = _Tracked()
        try:
            record.ref = weakref.ref(entry['native'])
        except TypeError:
            record.ref = None
        record.actor, record.local, record.affordance = actor, entry['localId'], entry['affordanceId']
        record.origin, record.visible, record.target = entry['origin'], entry['visible'], entry['target']
        record.state, record.since, record.closed = None, now, False
        self.tracked[record.local] = record
        return record

    def _outcome(self, native):
        if native is None:
            return 'unknown', None
        try:
            outcome = self.reader.outcome(native)
        except Exception as error:
            self.error = type(error).__name__
            return 'unknown', None
        if not isinstance(outcome, tuple) or len(outcome) != 2 or outcome[0] not in ENDINGS[:-1]:
            return 'unknown', None
        finishing = outcome[1]
        return outcome[0], finishing if isinstance(finishing, str) and finishing in FINISHING else None

    def _end(self, record, outcome, now, events):
        if record.closed:
            return
        record.closed = True
        extra = {'elapsedMs': max(0, min(int(round((now - record.since) * 1000)), MAX_ELAPSED_MS - 1))}
        if outcome[1] is not None:
            extra['finishingType'] = outcome[1]
        self._emit(record, outcome[0], events, extra)

    def _emit(self, record, state, events, extra=None):
        if self.seq >= MAX_SEQ - 1:
            self.dropped += 1
            return
        self.seq += 1
        event = {'kind': KIND, 'v': VERSION, 'streamId': self.stream_id, 'seq': self.seq, 'interactionId': record.local,
                 'actorSimId': record.actor, 'affordanceId': record.affordance, 'state': state, 'origin': record.origin,
                 'visible': record.visible, 'target': dict(record.target)}
        if extra:
            event.update(extra)
        if len(self.pending) >= MAX_EVENTS:
            self.pending.popleft()
            self.dropped += 1
        self.pending.append(event)
        self.recent.append(event)
        events.append(event)

    def drain(self, limit=MAX_EVENTS):
        """Retire et rend au plus `limit` événements, du plus ancien au plus récent."""
        count = min(limit, len(self.pending)) if type(limit) is int and limit > 0 else 0
        return [self.pending.popleft() for _ in range(count)]

    def status(self):
        return {'state': self.state, 'streamId': self.stream_id, 'actorSimId': self.actor, 'emitted': self.seq,
                'tracked': len(self.tracked), 'pending': len(self.pending), 'dropped': self.dropped,
                'overflow': self.overflow, 'rejected': self.rejected, 'unidentified': self.unidentified,
                'truncated': self.truncated, 'lastError': self.error, 'recent': [dict(event) for event in self.recent],
                'hookMode': 'poll', 'serverLink': 'missing', 'gameReadiness': 'NOT_TESTED_GAME'}


class GameInteractions:
    """Lecteur natif : super-interactions du Sim actif (SIState et interaction en cours de la file). Aucun setter."""

    def __init__(self, services=None, clones=dict, remote_sims=dict):
        # `clones` rend {foyer local: joueur} déjà validé (validated_clones) ; `remote_sims` rend
        # {(foyer local, Sim local): (joueur, Sim natif distant)} (validated_remote_sims). Sans les deux, aucun
        # clone n'est nommé.
        self.clones = clones
        self.remote_sims = remote_sims
        self.thread_id = threading.get_ident()
        if services is None:
            try:
                import services as game_services
            except ImportError:
                game_services = None
            services = game_services
        self.services = services

    def _main_thread(self):
        if threading.get_ident() != self.thread_id:
            raise RuntimeError('Lecture du jeu interdite hors thread principal')

    def read(self):
        """None sans Sim actif. Une API absente lève : l'observateur l'expose en `read_error`, jamais en silence."""
        self._main_thread()
        if self.services is None:
            return None
        sim = self.services.get_active_sim()
        if sim is None:
            return None
        actor = _number(sim.sim_info.sim_id)
        if actor is None:
            return None
        found = list(sim.si_state) if sim.si_state is not None else []
        running = sim.queue.running if sim.queue is not None else None
        if running is not None and all(running is not other for other in found):
            found.append(running)
        entries, unidentified = [], 0
        for interaction in found:
            if interaction.is_super is not True:
                continue
            number = interaction.pipeline_progress
            number = int(number) if isinstance(number, int) and not isinstance(number, bool) else None
            if number in (0, 1):
                continue
            local = _number(interaction.id)
            affordance = _number(getattr(interaction.affordance, 'guid64', None))
            if number not in PROGRESS or local is None or affordance is None:
                unidentified += 1
                continue
            entries.append({'native': interaction, 'localId': int(local), 'affordanceId': affordance,
                            'progress': PROGRESS[number], 'origin': self._origin(interaction),
                            'visible': interaction.visible is True, 'target': self._target(sim, interaction)})
        entries.sort(key=lambda entry: entry['localId'])
        return {'actorSimId': actor, 'interactions': entries[:MAX_READ], 'truncated': len(entries) > MAX_READ,
                'unidentified': unidentified}

    @staticmethod
    def _origin(interaction):
        try:
            if interaction.is_user_directed:
                return 'user'
            if interaction.is_autonomous:
                return 'autonomy'
            return 'script' if interaction.source is not None else 'unknown'
        except Exception:
            return 'unknown'

    def _target(self, sim, interaction):
        """Cible classée sans rien inventer : toute lecture impossible donne `unknown`."""
        try:
            target = interaction.target
            if target is None:
                return {'kind': 'none'}
            if target is sim:
                return {'kind': 'self'}
            if target.is_sim is True:
                return self._sim_target(target.sim_info)
            if target.is_terrain is True:
                return {'kind': 'terrain'}
            part = target.is_part is True
            definition = _number((target.part_owner if part else target).definition.id)
            if definition is not None:
                result = {'kind': 'object', 'definitionId': definition, 'part': part}
                try:
                    position = target.position
                    coordinates = [getattr(position, k) for k in ('x', 'y', 'z')]
                    if all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)
                           for v in coordinates):
                        placement = dict(zip(('x', 'y', 'z'), (int(round(v * 1000)) for v in coordinates)))
                        placement['level'] = target.routing_surface.secondary_id
                        candidate = dict(result, placement=placement)
                        if clean_target(candidate) is not None:
                            result = candidate
                except Exception:
                    pass  # Ancien objet sans position exploitable : contrat historique conservé.
                return result
            return {'kind': 'unknown'}
        except Exception:
            return {'kind': 'unknown'}

    def _sim_target(self, info):
        household_id = _number(info.household_id)
        if household_id is not None and household_id == _number(self.services.active_household_id()):
            sim_id = _number(info.sim_id)
            return {'kind': 'household_sim', 'simId': sim_id} if sim_id is not None else {'kind': 'unknown'}
        household = info.household
        description = household.description if household is not None else None
        if type(description) is not str or not description.startswith(PREFIX):
            # PNJ ou Sim d'un autre foyer de cette partie : son identifiant n'a de sens que dans cette sauvegarde.
            return {'kind': 'local_sim'}
        # Le marqueur seul ne prouve rien (texte modifiable, clone en attente) : il faut le foyer validé par
        # l'apparence reçue, pour le même joueur, et ce Sim local précis relié au Sim natif distant. Jamais le
        # premier Sim du foyer par défaut ; l'identifiant local du clone n'est jamais émis.
        marker = parse_description(description)
        player = self.clones().get(household_id) if household_id is not None else None
        if marker is None or not _player(player) or marker[0] != player:
            return {'kind': 'unknown'}
        remote = self.remote_sims().get((household_id, _number(info.sim_id)))
        if type(remote) is not tuple or len(remote) != 2 or remote[0] != player or not _decimal(remote[1]):
            return {'kind': 'unknown'}
        return {'kind': 'remote_clone', 'playerId': player, 'simId': remote[1]}

    def outcome(self, native):
        """(fin, finishingType) lus sur l'InteractionFinisher. Sans fin déclarée par le jeu : `unknown`."""
        self._main_thread()
        if not native.is_finishing:
            return 'unknown', None
        if native.has_been_reset:
            state = 'reset'
        elif native.is_finishing_naturally:
            state = 'finished'
        elif native.has_been_user_canceled:
            state = 'user_canceled'
        elif native.transition_failed:
            state = 'failed'
        elif native.has_been_canceled:
            state = 'canceled'
        else:
            state = 'unknown'
        name = getattr(native.finishing_type, 'name', None)
        name = name.lower() if isinstance(name, str) else None
        return state, name if name in FINISHING else None
