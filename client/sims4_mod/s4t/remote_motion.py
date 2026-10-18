"""Poses des autres joueurs appliquées à leur clone S4T sur le même terrain (12/10/2026).

Entrée : les poses de `game.join`, `game.state`, `game.presence` et `game.snapshot` (PresenceSync les relaie).
Sortie : le clone « Nom (S4T) » du joueur (remote_household) est instancié sur le terrain, puis marche vers la
position publiée. Il n'est jamais téléporté : un grand écart est refusé jusqu'à ce qu'un instantané confirme
la pose, et une pose sans étage n'entraîne ni spawn ni trajet. Un clone est reconnu par trois preuves : son foyer
porte `S4T:<playerId>:<sha16>`, cette empreinte est celle de l'apparence reçue de ce joueur, et la pose concerne
le Sim que cette apparence représente. Le Sim actif et le foyer local ne sont jamais des cibles.

API du jeu 1.128 relevée par désassemblage, jamais exécutée (docs/REMOTE_MOTION.md) : SimSpawner.spawn_sim,
(sim_location), affordance « aller ici » (CommandTuning.TERRAIN_GOHERE_AFFORDANCE), Sim.reset puis Sim.destroy.
Une action native par tick, sur le thread principal (alarme d'AutoLink). Seule l'instance créée ici est pilotée
ou retirée : la preuve garde le foyer, le Sim local et l'objet instance lui-même. Une instance apparue autrement
(remplacée par le jeu, Sim réutilisé, création non constatée) n'est ni déplacée ni détruite. Ni foyer ni SimInfo
n'est jamais supprimé. Aucune animation n'est rejouée ; l'orientation n'est appliquée qu'au spawn. NOT_TESTED_GAME.
"""
import math
import re
import uuid
import weakref

from .journal import log
from .remote_household import PENDING, parse_description

ARRIVE_DISTANCE = 0.75      # En deçà, le clone est réputé arrivé : aucune action.
REPLAN_DISTANCE = 1.0       # La cible doit avoir bougé d'autant pour replanifier un trajet en cours.
GAP_DISTANCE = 25.0         # Au-delà : grand écart, aucun mouvement avant qu'un instantané confirme la pose.
GAP_RESYNC_TICKS = 60       # Espacement des resynchronisations pour grand écart, par clone.
GAP_MAX_ASKS = 3            # Instantanés demandés au plus par épisode de grand écart resté sans confirmation.
MIN_LEVEL, MAX_LEVEL = -8, 16
ROUTE_RETRY_TICKS = 5
MAX_ROUTE_TRIES = 3         # Trajet refusé ou bloqué : arrêt, jamais de téléportation de secours.
ROUTE_RECOVERY_TICKS = 60   # Reprendre après une obstruction temporaire sans rafale native.
SPAWN_RETRY_TICKS = 30
MAX_ATTEMPTS = 3
GRACE_TICKS = 5             # Lien perdu : délai avant de retirer les instances.
STALE_TICKS = 30
SNAPSHOT_INTERVAL = 10
SNAPSHOT_ACK_TICKS = 15
SNAPSHOT_REFUSED_TICKS = 300
MAX_PARTICIPANTS = 4096
MAX_POSES = 256
MAX_DEAD_STREAMS = 64
MAX_COORDINATE = 1000000.0
_NATIVE = re.compile(r'^[1-9][0-9]{0,19}$')
_CODE = re.compile(r'^[a-z0-9_.]{1,64}$')


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def clean_pose(sim):
    """(simId, pose) d'un Sim reçu du serveur, ou None. Rien n'est corrigé : une valeur douteuse écarte la pose."""
    if not isinstance(sim, dict):
        return None
    sim_id, position, revision = sim.get('simId'), sim.get('position'), sim.get('revision')
    if not isinstance(sim_id, str) or not _NATIVE.match(sim_id) or type(revision) is not int or revision < 0:
        return None
    if not isinstance(position, dict) or not _number(sim.get('rotation')):
        return None
    values = [position.get(axis) for axis in ('x', 'y', 'z')]
    if not all(_number(v) and abs(v) <= MAX_COORDINATE for v in values):
        return None
    pose = {'revision': revision, 'position': dict(zip(('x', 'y', 'z'), (float(v) for v in values))),
            'rotation': float(sim['rotation']), 'animation': None}
    animation = sim.get('animation')
    if isinstance(animation, str) and 1 <= len(animation) <= 80:
        pose['animation'] = animation
    level = sim.get('level')
    if type(level) is int and MIN_LEVEL <= level <= MAX_LEVEL:
        pose['level'] = level  # Absent (client 0.1.18) ou hors bornes : étage inconnu, aucun mouvement natif.
    return sim_id, pose


def new_move():
    return {'target': None, 'tries': 0, 'wait': 0, 'stuck': False, 'gap': None, 'gap_wait': 0,
            'progress': None, 'idle': 0, 'asks': 0, 'origin': None}


def _far(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1]) > GAP_DISTANCE


def decide(current, pose, move, ready=True):
    """'none', 'route', 'level' (étage inconnu) ou 'gap' (grand écart à resynchroniser) pour un clone instancié.
    Jamais de téléportation. Un appel par tick ; `move` est l'état du trajet. `ready` : aucun instantané en vol
    ni en délai, condition d'une nouvelle demande pour un grand écart resté sans confirmation.

    Épisode de grand écart : il s'ouvre par une demande (asks=1). Tant qu'aucun instantané ne confirme la pose,
    une nouvelle demande part au plus tous les GAP_RESYNC_TICKS, quand `ready`, jusqu'à GAP_MAX_ASKS au total.
    Compteur réarmé seulement par : pose confirmée (le saut suivant ouvre un nouvel épisode), écart refermé,
    arrivée, nouvel état de trajet (reset, retrait ou adoption du clone), ou cible déplacée de plus de
    GAP_DISTANCE depuis l'ouverture de l'épisode (mouvement distinct). Aucun trajet sans pose confirmée."""
    for key in ('wait', 'gap_wait'):
        if move[key] > 0:
            move[key] -= 1
    level = pose.get('level')
    if type(level) is not int:
        return 'level'
    target = (pose['position']['x'], pose['position']['z'])
    here = (current['x'], current['z'])
    progress = move.get('progress')
    if progress is None or math.hypot(here[0] - progress[0], here[1] - progress[1]) >= .1:
        move.update(progress=here, idle=0, tries=0, stuck=False)
    else:
        move['idle'] = move.get('idle', 0) + 1
    other_level = level != current.get('level')
    if not other_level and math.hypot(target[0] - here[0], target[1] - here[1]) <= ARRIVE_DISTANCE:
        move.update(target=None, tries=0, stuck=False, gap=None, asks=0, origin=None)
        return 'none'
    if not _far(target, here):
        move.update(gap=None, asks=0, origin=None)
    elif move['gap'] == 'asked' and not (move['asks'] >= GAP_MAX_ASKS and _far(target, move['origin'])):
        # Instantané perdu, refusé, ancien ou sans ce clone : nouvelle demande espacée et bornée, jamais de trajet.
        if move['gap_wait'] > 0 or not ready or move['asks'] >= GAP_MAX_ASKS:
            return 'none'
        move.update(asks=move['asks'] + 1, gap_wait=GAP_RESYNC_TICKS)
        return 'gap'
    elif move['gap'] == 'asked' or move['gap'] is None or _far(target, move['gap']):
        # Déplacement rapide : rien ne bouge tant qu'un instantané n'a pas confirmé une pose proche de celle-ci.
        if move['gap_wait'] > 0:
            return 'none'
        move.update(target=None, tries=0, stuck=False, gap='asked', gap_wait=GAP_RESYNC_TICKS, asks=1, origin=target)
        return 'gap'
    last = move['target']
    if last is None or math.hypot(target[0] - last[0], target[1] - last[1]) > REPLAN_DISTANCE:
        move.update(target=target, tries=1, wait=ROUTE_RETRY_TICKS, stuck=False)
        return 'route'
    if move['stuck'] and move['wait'] == 0:
        move.update(tries=0, stuck=False)
    if (current.get('moving') and move['idle'] < ROUTE_RETRY_TICKS) or move['stuck'] or move['wait'] > 0:
        return 'none'
    if move['tries'] >= MAX_ROUTE_TRIES:
        move['stuck'] = True
        move['wait'] = ROUTE_RECOVERY_TICKS
        return 'none'
    move['tries'] += 1
    move['wait'] = ROUTE_RETRY_TICKS
    return 'route'


def _own(household, sim_id, instance):
    """Preuve de possession d'une instance. La référence faible ne retient pas un objet que le jeu a détruit ;
    un objet qui n'en accepte pas lève TypeError : identité incertaine, donc pas de possession."""
    return {'household': household, 'sim': sim_id, 'instance': weakref.ref(instance)}


def replace_route(sim, affordance, target, context, finish_type):
    """Une seule interaction de trajet S4T par copie ; aucune autre action n'est annulée."""
    previous = getattr(sim, '_s4t_motion_route', None)
    if previous is not None:
        previous.cancel(finish_type, cancel_reason_msg='S4Together: owner route superseded')
        sim._s4t_motion_route = None
    result = sim.push_super_affordance(affordance, target, context)
    if not result:
        raise RuntimeError('route_refused')
    sim._s4t_motion_route = getattr(result, 'interaction', None)


class RemoteMotion:
    """Appelée par PresenceSync (réception) et AutoLink (tick). Jamais d'exception vers le jeu."""

    def __init__(self, adapter=None, new_id=lambda: str(uuid.uuid4())):
        self.adapter = adapter if adapter is not None else GameMotion()
        self.new_id = new_id
        self.rejected = {}
        self.spawned = self.routed = self.gaps = self.despawned = self.abandoned = self.adopted = 0
        self.applied = self.animations = self.unrepresented = 0
        self.snapshots = {'requested': 0, 'applied': 0, 'refused': 0, 'timeouts': 0, 'stale': 0}
        self.attempts = {}       # playerId -> spawns ratés ; gardé d'une zone à l'autre (pas de boucle d'échecs).
        self.waiting = None      # (requestId, ticks restants) du game.snapshot en vol.
        self.cooldown = 0
        self.away = 0
        self._forget_instances()
        self._forget_view()

    # -- Cycle de vie -----------------------------------------------------------------------------------

    def reset(self):
        """Zone chargée ou déchargée : le jeu a détruit les instances avec la zone. Aucun appel natif."""
        self._forget_instances()
        self._forget_view()

    def _forget_instances(self):
        self.owned = {}          # playerId -> preuve (_own) de l'instance créée ici : la seule pilotée ou retirée.
        self.foreign = set()     # Joueurs dont le clone a une instance qui n'est pas la nôtre : laissée telle quelle.
        self.retry = {}
        self.releases = {}
        self.moves = {}
        self.unlevel = set()     # Joueurs dont la pose n'a pas d'étage : ni spawn ni trajet.
        self.mapping = {}        # (playerId, simId distant) -> (foyer local, Sim local)
        self.represented = {}    # Joueur -> (foyer local, Sim distant, empreinte appliquée prouvée).

    def _forget_view(self):
        self.zone_id = None
        self.own = None
        self.participants = set()
        self.streams = {}        # playerId -> streamId confirmé par une présence
        self.dead = []           # (playerId, streamId) périmés : jamais repris avant un nouveau join
        self.suspects = {}       # playerId -> (streamId inconnu, séquence de l'état qui le portait)
        self.marks = {}          # playerId -> séquence serveur de son dernier enregistrement de présence
        self.base = None         # Séquence de l'ACK du join ou du dernier instantané appliqué.
        self.poses = {}          # (playerId, simId) -> pose
        self.need = set()
        self.quiet = 0

    def _reject(self, code):
        self.rejected[code] = self.rejected.get(code, 0) + 1

    def recover_view(self, manager, zone_id, own_player):
        """Présence déjà confirmée mais vue perdue : relire le serveur, jamais inventer une pose.
        Appelé uniquement par AutoLink avec la zone confirmée de PresenceSync.
        Les instances existantes ne sont pas retirées pendant cette récupération.
        """
        if self.zone_id is not None or not isinstance(zone_id, str) or not zone_id.startswith('native:'):
            return False
        if not isinstance(own_player, str) or not own_player:
            return False
        if self.waiting is not None:
            manager.forget(self.waiting[0])
            self.waiting = None
        self._forget_view()
        self.zone_id, self.own, self.cooldown = zone_id, own_player, 0
        self.need.add('view_lost')
        log('remote_motion', 'view_recovery')
        return True

    # -- Réception --------------------------------------------------------------------------------------

    def on_join(self, zone_id, payload):
        """ACK de game.join : vue initiale de la zone. Les instances déjà créées survivent (reconnexion)."""
        try:
            self._forget_view()
            payload = payload if isinstance(payload, dict) else {}
            own = payload.get('presence')
            self.zone_id = zone_id
            self.own = own.get('playerId') if isinstance(own, dict) else None
            self.base = self._sequence(payload)
            self._view(payload, self.base)
            self._confirm(payload, self.base)
            if not isinstance(payload.get('presences'), list):
                self.need.add('join')  # Serveur sans streamId : un instantané est demandé une fois.
        except Exception as error:
            log('remote_motion', 'join:' + type(error).__name__)

    def on_event(self, kind, payload):
        """Événement `state` ou `presence` reçu par PresenceSync. Filtrage strict sur la zone jointe."""
        try:
            if isinstance(payload, dict) and self.zone_id is not None:
                self._state(payload) if kind == 'state' else self._presence(payload)
        except Exception as error:
            log('remote_motion', kind + ':' + type(error).__name__)

    @staticmethod
    def _sequence(payload):
        value = payload.get('sequence')
        return value if type(value) is int and value >= 0 else None

    def _fresh(self, player, sequence):
        """Faux si le message est plus ancien que le dernier enregistrement de présence de ce joueur. La séquence
        globale ne sert qu'à comparer deux âges : un trou ne signifie rien et ne demande aucun instantané."""
        if sequence is None:
            return player not in self.marks
        return sequence >= self.marks.get(player, -1)

    def _mark(self, player, sequence):
        if sequence is not None and sequence > self.marks.get(player, -1):
            if len(self.marks) >= MAX_PARTICIPANTS and player not in self.marks:
                self.marks = {p: s for p, s in self.marks.items() if p in self.participants}
            self.marks[player] = sequence

    def _view(self, payload, sequence):
        """Vue de la zone portée par un ACK, une présence ou un instantané. Ce qui concerne un joueur dont un
        enregistrement plus récent est déjà connu est laissé tel quel (instantané ancien lu après une présence)."""
        stale = set()
        participants = payload.get('participants')
        if isinstance(participants, list) and len(participants) <= MAX_PARTICIPANTS:
            listed = {p for p in participants if isinstance(p, str)}
            stale = {p for p in listed | self.participants if not self._fresh(p, sequence)}
            self.participants = (listed - stale) | (self.participants & stale)
            self._prune()
        presences = payload.get('presences')
        for presence in presences if isinstance(presences, list) else []:
            if not isinstance(presence, dict) or presence.get('zoneId') != self.zone_id:
                continue
            if self._fresh(presence.get('playerId'), sequence):
                self._stream(presence.get('playerId'), presence.get('streamId'), sequence)
            else:
                stale.add(presence.get('playerId'))
        for sim in self._sims(payload):
            owner = sim.get('ownerId', sim.get('playerId'))
            if owner != self.own and owner in self.participants:
                if self._fresh(owner, sequence):
                    self._pose(owner, sim)
                else:
                    stale.add(owner)
        if stale:
            self._reject('stale_record')

    def _prune(self):
        for key in [k for k in self.poses if k[0] not in self.participants]:
            del self.poses[key]
        for player in [p for p in self.streams if p not in self.participants]:
            self._retire(player)

    def _retire(self, player):
        stream = self.streams.pop(player, None)
        if stream is not None:
            self.dead = (self.dead + [(player, stream)])[-MAX_DEAD_STREAMS:]
        for key in [k for k in self.poses if k[0] == player]:
            del self.poses[key]

    def _stream(self, player, stream, sequence):
        """Nouveau flux d'un joueur : ses révisions repartent, l'ancien flux est ignoré désormais. Un flux déjà
        périmé n'est jamais repris, quel que soit le message qui le porte : chaque join en crée un nouveau."""
        if not isinstance(player, str) or not isinstance(stream, str) or not 1 <= len(stream) <= 64:
            return
        if player == self.own:
            return
        if (player, stream) in self.dead:
            return self._reject('dead_stream')
        self._mark(player, sequence)
        if self.streams.get(player) == stream:
            return
        self._retire(player)
        self.streams[player] = stream

    @staticmethod
    def _sims(payload):
        """Sims d'un message, à plat : `sims` direct ou sous `data`, entrée nue ou enveloppée {sim, sequence}."""
        sims = payload.get('sims')
        if not isinstance(sims, list) and isinstance(payload.get('data'), dict):
            sims = payload['data'].get('sims')
        result = []
        for entry in sims[:MAX_POSES] if isinstance(sims, list) else []:
            if isinstance(entry, dict):
                result.append(entry['sim'] if isinstance(entry.get('sim'), dict) else entry)
        return result

    def _pose(self, player, sim):
        cleaned = clean_pose(sim)
        if cleaned is None:
            return self._reject('pose_shape')
        key = (player, cleaned[0])
        known = self.poses.get(key)
        if known is not None and cleaned[1]['revision'] <= known['revision']:
            return self._reject('stale_revision')
        if known is None and len(self.poses) >= MAX_POSES:
            return self._reject('pose_limit')
        self.poses[key] = cleaned[1]
        self.applied += 1
        if cleaned[1]['animation'] is not None:
            self.animations += 1

    def _state(self, payload):
        player, stream = payload.get('playerId'), payload.get('streamId')
        if payload.get('zoneId') != self.zone_id:
            return self._reject('zone_mismatch')
        if player == self.own:
            return self._reject('own_player')
        if player not in self.participants:
            self.need.add('not_participant')
            return self._reject('not_participant')
        known = self.streams.get(player)
        if stream is None:
            if known is not None:
                return self._reject('missing_stream')
        elif (player, stream) in self.dead:
            return self._reject('old_stream')
        elif known != stream and (known is not None or self.streams):
            # Flux inconnu : ni adopté ni appliqué ; l'instantané dira lequel est vivant.
            self.suspects[player] = (stream, self._sequence(payload))
            self.need.add('unknown_stream')
            return self._reject('unknown_stream')
        for sim in self._sims(payload):
            if sim.get('ownerId', sim.get('playerId', player)) != player:
                self._reject('owner_mismatch')
            else:
                self._pose(player, sim)

    def _presence(self, payload):
        presence = payload.get('presence')
        if not isinstance(presence, dict) or not isinstance(presence.get('playerId'), str):
            return
        player, sequence = presence['playerId'], self._sequence(payload)
        if player == self.own:
            return
        if not self._fresh(player, sequence):
            return self._reject('stale_presence')
        self._mark(player, sequence)
        if presence.get('zoneId') == self.zone_id and presence.get('online') is not False:
            self.participants.add(player)
            self._stream(player, presence.get('streamId'), sequence)
            self._view(payload, sequence)
        elif payload.get('previousZoneId') == self.zone_id or presence.get('zoneId') == self.zone_id:
            self.participants.discard(player)
            self._retire(player)

    def on_messages(self, manager, messages):
        """Réponse à game.snapshot. Un instantané d'une autre zone que celle jointe est écarté."""
        for message in messages if isinstance(messages, list) else []:
            if self.waiting is None or not isinstance(message, dict) or message.get('id') != self.waiting[0]:
                continue
            self.waiting = None
            payload = message.get('payload')
            if message.get('ok') is not True or not isinstance(payload, dict):
                code = message.get('code')
                log('remote_motion', 'snapshot:' + (code if isinstance(code, str) and _CODE.match(code) else 'error'))
                self.snapshots['refused'] += 1
                self.cooldown = SNAPSHOT_REFUSED_TICKS
                self.need.clear()
            elif self.zone_id is None or payload.get('zoneId') != self.zone_id:
                self._reject('snapshot_zone')
            else:
                self._snapshot(payload)

    def _snapshot(self, payload):
        sequence = self._sequence(payload)
        self.cooldown = SNAPSHOT_INTERVAL
        if self.base is not None and (sequence is None or sequence < self.base):
            # Demandé avant le join en cours (retour sur la zone, reconnexion) : rien n'en est repris.
            self.snapshots['stale'] += 1
            self.need.discard('large_gap')
            return self._reject('stale_snapshot')
        self.snapshots['applied'] += 1
        self.base = sequence if sequence is not None else self.base
        self.need.clear()
        self.quiet = 0
        self._view(payload, sequence)
        self._confirm(payload, sequence)
        kept = {}
        for player, (stream, seen) in self.suspects.items():
            if self.streams.get(player) == stream:
                continue
            if sequence is not None and seen is not None and sequence < seen:
                # Instantané plus ancien que l'état qui portait ce flux : il ne peut pas le condamner.
                kept[player] = (stream, seen)
                self.need.add('unknown_stream')
            else:
                self.dead = (self.dead + [(player, stream)])[-MAX_DEAD_STREAMS:]
        self.suspects = kept

    def _confirm(self, payload, sequence):
        """Grand écart en attente : la pose que l'instantané porte pour ce clone devient la cible confirmée."""
        seen = {}
        for sim in self._sims(payload):
            cleaned = clean_pose(sim)
            if cleaned is not None and self._fresh(sim.get('ownerId', sim.get('playerId')), sequence):
                seen[(sim.get('ownerId', sim.get('playerId')), cleaned[0])] = cleaned[1]['position']
        for (player, sim_id) in self.mapping:
            move, position = self.moves.get(player), seen.get((player, sim_id))
            if move is not None and move['gap'] == 'asked' and position is not None:
                move['gap'] = (position['x'], position['z'])

    # -- Boucle -----------------------------------------------------------------------------------------

    def tick(self, manager, zone_id, own_player, ghosts, appearances):
        """zone_id : zone confirmée de PresenceSync, None hors ligne. ghosts : RemoteHouseholds.ghosts.
        appearances : AppearanceSync.remote. Une action native au plus."""
        try:
            self._tick(manager, zone_id, own_player, ghosts if isinstance(ghosts, dict) else {},
                       appearances if isinstance(appearances, dict) else {})
        except Exception as error:
            log('remote_motion', 'tick:' + type(error).__name__)
            self._reject('tick:' + type(error).__name__)

    def _tick(self, manager, zone_id, own_player, ghosts, appearances):
        if zone_id is None or zone_id != self.zone_id:
            self._forget_view()
        joined = self.zone_id is not None
        if joined:
            self.own = own_player if isinstance(own_player, str) else self.own
            self.away = 0
        else:
            self.away = min(self.away + 1, GRACE_TICKS + 1)
        try:
            self._ask(manager, joined)
        except Exception as error:
            # File pleine ou lien coupé : la demande reste due, le nettoyage ci-dessous a lieu quand même.
            self._reject('snapshot:' + type(error).__name__)
        if 'view_lost' in self.need:
            return  # Attendre le snapshot avant tout mouvement ou retrait d'une instance existante.
        targets = self._targets(ghosts, appearances) if joined else {}
        self.mapping = {k: v for k, v in self.mapping.items() if k[0] in targets and k[1] == targets[k[0]][1]}
        if not self._cleanup(joined, ghosts, appearances) and joined:
            self._move(targets)

    def _ask(self, manager, joined):
        """game.snapshot : une demande à la fois, espacée, seulement pour une raison (jamais en cadence)."""
        if self.cooldown > 0:
            self.cooldown -= 1
        if self.waiting is not None:
            request_id, left = self.waiting
            self.waiting = (request_id, left - 1)
            if left <= 1 or not joined:
                self.waiting = None
                self.snapshots['timeouts'] += 1
                self.cooldown = SNAPSHOT_REFUSED_TICKS
                self.need.discard('large_gap')  # Un grand écart ne redemande jamais en boucle.
                manager.forget(request_id)
            return
        if not joined:
            return
        quiet = any(p != self.own and not any(k[0] == p for k in self.poses) for p in self.participants)
        self.quiet = self.quiet + 1 if quiet else 0
        if self.quiet == STALE_TICKS:
            self.need.add('stale')
        if self.need and self.cooldown == 0:
            request_id = self.new_id()
            self.cooldown = SNAPSHOT_INTERVAL
            manager.request('game.snapshot', {}, request_id)
            self.waiting = (request_id, SNAPSHOT_ACK_TICKS)
            self.snapshots['requested'] += 1

    def _targets(self, ghosts, appearances):
        """{playerId: (foyer local, simId distant, pose)} des clones prouvés qui ont une pose à suivre."""
        targets, unrepresented = {}, 0
        for player in sorted(self.participants):
            if player == self.own:
                continue
            ghost, appearance = ghosts.get(player), appearances.get(player)
            sim_id = self._represented_for(player, ghost, appearance)
            unrepresented += sum(1 for key in self.poses if key[0] == player and key[1] != sim_id)
            if sim_id is not None and (player, sim_id) in self.poses:
                targets[player] = (ghosts[player]['householdId'], sim_id, self.poses[(player, sim_id)])
        self.unrepresented = unrepresented
        return targets

    def _represented_for(self, player, ghost, appearance):
        sim_id = self._represented(ghost, appearance)
        if sim_id is not None:
            self.represented[player] = (ghost['householdId'], sim_id, ghost['sha16'])
            return sim_id
        if isinstance(ghost, dict) and isinstance(appearance, dict):
            # Une nouvelle tenue ne doit pas rompre l'identité du clone déjà prouvée.
            proof = self.represented.get(player)
            expected = (ghost.get('householdId'), appearance.get('simId'), ghost.get('sha16'))
            if proof is not None and proof == expected and ghost.get('sha16') != PENDING:
                return appearance['simId']
        return None

    @staticmethod
    def _represented(ghost, appearance):
        """Sim distant que ce clone représente, ou None tant que foyer et apparence ne concordent pas."""
        if not isinstance(ghost, dict) or not isinstance(appearance, dict):
            return None
        sha16, sha256, sim_id = ghost.get('sha16'), appearance.get('sha256'), appearance.get('simId')
        if not isinstance(sha16, str) or not isinstance(sha256, str) or sha16 == PENDING or sha256[:16] != sha16:
            return None
        if not isinstance(sim_id, str) or not isinstance(ghost.get('householdId'), str):
            return None
        return sim_id

    def _cleanup(self, joined, ghosts, appearances):
        """Retire une instance créée ici dont le joueur n'est plus là. Vrai si le tick est consommé."""
        for player in sorted(self.owned):
            if joined:
                ghost, appearance = ghosts.get(player), appearances.get(player)
                # Identité changée ou jamais prouvée : rendre uniquement notre instance.
                changed = (isinstance(ghost, dict) and isinstance(appearance, dict)
                           and self._represented_for(player, ghost, appearance) is None)
                if player in self.participants and not changed:
                    continue
            elif self.away <= GRACE_TICKS:
                continue
            self._release(player)
            return True
        return False

    def _mine(self, player, household, current):
        """Vrai si l'instance relue est exactement celle créée ici : même foyer, même Sim local, même objet."""
        record, instance = self.owned.get(player), current.get('instance')
        return (record is not None and instance is not None and record['household'] == household
                and record['sim'] == str(current.get('localSimId')) and record['instance']() is instance)

    def _release(self, player):
        record = self.owned.get(player)
        if record is None:
            return self._forget(player)
        try:
            current = self.adapter.locate(player, record['household'])
        except Exception as error:
            # Foyer disparu ou plus reconnu comme clone : rien n'est touché.
            log('remote_motion', 'release:' + type(error).__name__)
            return self._forget(player)
        if not current.get('instanced'):
            return self._forget(player)
        if record.get('adopted') and self._mine(player, record['household'], current):
            # Créée par le jeu (le clone habite ici) : elle reste sur le terrain comme un habitant.
            return self._forget(player)
        if not self._mine(player, record['household'], current):
            # Instance remplacée par le jeu ou Sim réutilisé : ce n'est plus la nôtre, elle reste sur le terrain.
            self.abandoned += 1
            self._reject('release:not_owned')
            log('remote_motion', 'release:not_owned')
            return self._forget(player)
        try:
            self.adapter.despawn(player, record['household'], current['instance'])
        except Exception as error:
            self._reject('despawn:' + type(error).__name__)
            self.releases[player] = self.releases.get(player, 0) + 1
            if self.releases[player] >= MAX_ATTEMPTS:
                self.abandoned += 1
                self._forget(player)
            return
        self.despawned += 1
        log('remote_motion', 'despawn:ok')
        self._forget(player)

    def _forget(self, player):
        self.represented.pop(player, None)
        for store in (self.owned, self.releases, self.moves, self.retry):
            store.pop(player, None)
        self.unlevel.discard(player)
        self.foreign.discard(player)
        self.mapping = {k: v for k, v in self.mapping.items() if k[0] != player}

    def _move(self, targets):
        for player in sorted(targets):
            household, sim_id, pose = targets[player]
            try:
                current = self.adapter.locate(player, household)
            except Exception as error:
                self._reject('clone:' + type(error).__name__)
                continue
            self.mapping[(player, sim_id)] = (household, str(current.get('localSimId')))
            if 'level' not in pose:
                # Étage de destination inconnu : ni spawn ni trajet, jamais d'étage supposé.
                if player not in self.unlevel:
                    self.unlevel.add(player)
                    self._reject('level_unknown')
                    log('remote_motion', 'level_unknown')
                continue
            self.unlevel.discard(player)
            if not current.get('instanced'):
                self.foreign.discard(player)
                if player in self.owned:
                    # Notre instance a quitté le terrain sans nous : preuve caduque, nouvel essai plus tard.
                    self._forget(player)
                    self.retry[player] = SPAWN_RETRY_TICKS
                    self._reject('instance_lost')
                    continue
                if self._spawn(player, household, pose, current):
                    return
                continue
            if not self._mine(player, household, current) and not self._adopt(player, household, current):
                continue
            action = decide(current, pose, self.moves.setdefault(player, new_move()),
                            ready=self.waiting is None and self.cooldown == 0)
            if action == 'gap':
                self.gaps += 1
                self.need.add('large_gap')
                self._reject('large_gap')
                log('remote_motion', 'large_gap')
            if action != 'route':
                continue
            try:
                self.adapter.route(player, household, pose, current['instance'])
                self.routed += 1
            except Exception as error:
                self._reject('route:' + type(error).__name__)
            return

    def _adopt(self, player, household, current):
        """Instance du clone créée par le jeu (le clone habite ce terrain, rechargement de zone) : _clone a déjà
        prouvé foyer S4T de ce joueur, Sim unique, non jouable. Elle est pilotée mais jamais retirée par nous."""
        try:
            self.owned[player] = dict(_own(household, str(current.get('localSimId')), current['instance']), adopted=True)
        except TypeError:
            self._not_ours(player)
            return False
        self.moves.pop(player, None)
        self.foreign.discard(player)
        self.adopted += 1
        log('remote_motion', 'adopted')
        return True

    def _not_ours(self, player):
        """Le clone a une instance qui n'est pas celle créée ici : ni trajet ni retrait, abandon signalé une fois."""
        if self.owned.pop(player, None) is not None:
            self.moves.pop(player, None)
        if player not in self.foreign:
            self.foreign.add(player)
            self._reject('instance_not_owned')
            log('remote_motion', 'instance_not_owned')

    def _spawn(self, player, household, pose, current):
        """Vrai si un appel natif est parti. L'adaptateur rend l'instance qu'il a constatée juste après l'appel,
        y compris quand le spawn échoue en route (attribut `instance` de l'exception) : elle seule est possédée."""
        if self.attempts.get(player, 0) >= MAX_ATTEMPTS:
            return False
        if self.retry.get(player, 0) > 0:
            self.retry[player] -= 1
            return False
        failure = None
        try:
            instance = self.adapter.spawn(player, household, pose)
        except Exception as error:
            instance, failure = getattr(error, 'instance', None), type(error).__name__
        if instance is not None:
            try:
                self.owned[player] = _own(household, str(current.get('localSimId')), instance)
            except TypeError:
                instance, failure = None, failure or 'unidentified'
        if instance is None:
            # Rien de constaté : si une instance apparaît plus tard, son origine est incertaine (_not_ours).
            self._spawn_failed(player, failure or 'unconfirmed')
        elif failure is not None:
            # Créée puis erreur : l'instance est à nous, donc retirée au départ du joueur.
            self._reject('spawn:' + failure)
            log('remote_motion', 'spawn:created_then_' + failure)
        if instance is not None:
            self.attempts.pop(player, None)
            self.spawned += 1
            log('remote_motion', 'spawn:ok')
        return True

    def _spawn_failed(self, player, name):
        self.attempts[player] = self.attempts.get(player, 0) + 1
        self.retry[player] = SPAWN_RETRY_TICKS
        self._reject('spawn:' + name)
        log('remote_motion', 'spawn:' + name)

    # -- Diagnostic -------------------------------------------------------------------------------------

    def status(self):
        """Compteurs pour s4t.diagnostic : ni joueur, ni Sim, ni foyer, ni flux, ni position."""
        return {'mode': 'native' if isinstance(self.adapter, GameMotion) else 'fixture', 'zoneId': self.zone_id,
                'participants': len(self.participants), 'streams': len(self.streams), 'poses': len(self.poses),
                'posesApplied': self.applied, 'mapped': len(self.mapping), 'unrepresented': self.unrepresented,
                'instances': len(self.owned), 'foreign': len(self.foreign), 'rotation': 'spawn_only',
                'spawned': self.spawned, 'adopted': self.adopted, 'routed': self.routed,
                'teleported': 0, 'largeGaps': self.gaps, 'levelUnknown': len(self.unlevel),
                'gapPending': sum(1 for move in self.moves.values() if move['gap'] == 'asked'), 'despawned': self.despawned, 'abandoned': self.abandoned,
                'failed': sum(1 for n in self.attempts.values() if n >= MAX_ATTEMPTS),
                'stuck': sum(1 for move in self.moves.values() if move['stuck']),
                'animationsSeen': self.animations, 'animation': 'observed_not_played',
                'rejected': dict(self.rejected),
                'snapshot': dict(self.snapshots, pending=self.waiting is not None, need=sorted(self.need)),
                'gameReadiness': 'NOT_TESTED_GAME'}


class GameMotion:
    """Adaptateur du jeu. Chaque méthode revalide le clone puis lève en cas d'échec ; RemoteMotion compte."""

    CAUSE = 'S4Together: remote player left the lot'

    def _clone(self, player_id, household_id):
        """SimInfo du clone de ce joueur exactement ; lève pour le foyer local, le Sim actif ou tout autre foyer."""
        import services
        household = services.household_manager().get(int(household_id))
        if household is None:
            raise LookupError('household')
        parsed = parse_description(getattr(household, 'description', None))
        if parsed is None or parsed[0] != player_id or parsed[1] == PENDING:
            raise PermissionError('not_remote_household')
        if household.id == services.active_household_id():
            raise PermissionError('local_household')
        sim_infos = list(household.sim_info_gen())
        if len(sim_infos) != 1:
            raise LookupError('sim_info')
        sim_info = sim_infos[0]
        active = services.active_sim_info()
        if sim_info.is_selectable or (active is not None and active.sim_id == sim_info.sim_id):
            raise PermissionError('local_sim')
        return sim_info

    def _sim(self, player_id, household_id):
        from objects import ALL_HIDDEN_REASONS
        sim_info = self._clone(player_id, household_id)
        return sim_info, sim_info.get_sim_instance(allow_hidden_flags=ALL_HIDDEN_REASONS)

    def _instance(self, player_id, household_id):
        sim = self._sim(player_id, household_id)[1]
        if sim is None:
            raise LookupError('not_instanced')
        return sim

    def locate(self, player_id, household_id):
        import sims4.math
        sim_info, sim = self._sim(player_id, household_id)
        if sim is None:
            return {'localSimId': str(sim_info.sim_id), 'instanced': False}
        position = sim.position
        return {'localSimId': str(sim_info.sim_id), 'instanced': True, 'x': float(position.x), 'y': float(position.y),
                'z': float(position.z), 'rotation': float(sims4.math.yaw_quaternion_to_angle(sim.orientation)),
                'instance': sim,
                'level': int(sim.routing_surface.secondary_id), 'moving': bool(sim.is_moving)}

    @staticmethod
    def _vector(pose):
        import sims4.math
        position = pose['position']
        return sims4.math.Vector3(position['x'], position['y'], position['z'])

    @staticmethod
    def _surface(pose):
        """Surface de l'étage reçu. Étage absent ou hors bornes : refus, jamais 0 ni l'étage local supposé."""
        import routing
        import services
        level = pose.get('level')
        if type(level) is not int or not MIN_LEVEL <= level <= MAX_LEVEL:
            raise ValueError('level_unknown')
        return routing.SurfaceIdentifier(services.current_zone_id(), level, routing.SurfaceType.SURFACETYPE_WORLD)

    def spawn(self, player_id, household_id, pose):
        """SimSpawner.spawn_sim avec sim_location : position, orientation et étage reçus ; le jeu cherche une
        place libre proche sur cette surface (SimInfo.create_sim_instance, placement FGL)."""
        import sims4.math
        from sims.sim_spawner import SimSpawner
        surface = self._surface(pose)
        sim_info, sim = self._sim(player_id, household_id)
        if sim is not None:
            return None  # Déjà sur le terrain par une autre voie : cette instance n'est pas la nôtre.
        orientation = sims4.math.angle_to_yaw_quaternion(pose['rotation'])
        location = sims4.math.Location(sims4.math.Transform(self._vector(pose), orientation), surface)
        try:
            accepted = SimSpawner.spawn_sim(sim_info, sim_location=location)
        except Exception as error:
            error.instance = self._created(sim_info)
            raise
        created = self._created(sim_info)
        if accepted is False:
            # spawn_sim avale ses exceptions et rend False, parfois après create_object : l'instance est signalée.
            error = RuntimeError('spawn_refused')
            error.instance = created
            raise error
        return created

    @staticmethod
    def _created(sim_info):
        """Instance produite par l'appel qui vient de finir : absente juste avant (même thread, appel synchrone,
        SimInfo.create_sim_instance pose _sim_ref dès create_object), relue juste après. None si rien de constaté."""
        try:
            from objects import ALL_HIDDEN_REASONS
            return sim_info.get_sim_instance(allow_hidden_flags=ALL_HIDDEN_REASONS)
        except Exception:
            return None

    def _exact(self, player_id, household_id, instance):
        """L'instance actuelle du clone, seulement si c'est l'objet attendu."""
        sim = self._instance(player_id, household_id)
        if instance is None or sim is not instance:
            raise PermissionError('instance_changed')
        return sim

    def route(self, player_id, household_id, pose, instance):
        """Séquence de la triche sims.gohere : interaction « aller ici » poussée sur le clone, à l'étage reçu."""
        import sims4.math
        from interactions.context import InteractionContext
        from interactions.priority import Priority
        from interactions.interaction_finisher import FinishingType
        from objects.terrain import TerrainPoint
        from server.pick_info import PickInfo, PickType
        from server_commands.sim_commands import CommandTuning
        surface = self._surface(pose)
        sim = self._exact(player_id, household_id, instance)
        position = self._vector(pose)
        target = TerrainPoint(sims4.math.Location(sims4.math.Transform(position), surface))
        pick = PickInfo(pick_type=PickType.PICK_TERRAIN, target=target, location=position, routing_surface=surface)
        context = InteractionContext(sim, InteractionContext.SOURCE_SCRIPT_WITH_USER_INTENT, Priority.High, pick=pick,
                                     group_id=1)
        replace_route(sim, CommandTuning.TERRAIN_GOHERE_AFFORDANCE, target, context, FinishingType.USER_CANCEL)

    def despawn(self, player_id, household_id, instance):
        """Retire l'instance du terrain. Le SimInfo et le foyer restent : jamais de remove_permanently."""
        from objects.object_enums import ResetReason
        sim = self._exact(player_id, household_id, instance)
        sim.reset(ResetReason.RESET_EXPECTED, None, self.CAUSE)
        sim.destroy(source=sim, cause=self.CAUSE)
