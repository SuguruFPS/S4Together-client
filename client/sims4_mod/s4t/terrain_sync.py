"""Transport des terrains : publication du nôtre, réception de ceux des autres (13/10/2026).

Contrat de docs/TERRAIN_SYNC_0.1.32.md (terrain.publish paginé, terrain.updated, terrain.get, terrain.catalog),
implémenté par le serveur. Machine à états pure : le jeu n'est atteint que par `locate` et `read`
(lecture seule, terrain_adapter) et par `apply` (terrain_apply). Sans applicateur, un snapshot reçu et validé
reste à l'état « ready » : rien n'est écrit dans la partie. Murs, sols, toits et relief ne sont pas transportés.
NOT_TESTED_GAME, aucune latence mesurée.
"""
import re
import uuid

from . import terrain_adapter as terrain
from .home_lots import own_lot, remote_homes
from .journal import log

CHECKPOINT_TICKS = 2
REFRESH_LOCAL_TICKS = 2
REFRESH_TICKS = 30
RETRY_TICKS = 5
MAX_APPLY_ATTEMPTS = 3
APPLY_RECOVERY_TICKS = 60
FLIGHT_TICKS = 30
MAX_STALE = 3
MAX_REMOTE = 16
MAX_REVISION = 2 ** 53
_HASH = re.compile(r'^[0-9a-f]{64}$')
_CODE = re.compile(r'^[a-z_]{1,40}$')


def _revision(value):
    return value if type(value) is int and 0 <= value <= MAX_REVISION else None


def _last_apply(apply):
    """Compteurs de la dernière application (TerrainApplier.last), copiés ; None sans applicateur."""
    last = getattr(getattr(apply, '__self__', None), 'last', None)
    return dict(last) if isinstance(last, dict) else None


def _code(message):
    code = message.get('code')
    return code if isinstance(code, str) and _CODE.fullmatch(code) else 'invalid_request'


class TerrainSync:
    """Appelée par AutoLink à chaque relevé ; jamais d'exception vers le jeu, une publication et une demande en vol."""

    def __init__(self, locate, read, apply=None, new_id=lambda: str(uuid.uuid4())):
        self.locate = locate
        self.read = read
        self.apply = apply
        self.new_id = new_id
        self.rejected = {}
        self.reset()

    def reset(self, manager=None):
        """Autre zone, reconnexion ou nouvel arrivant : rien en vol n'est repris, tout est redemandé et republié."""
        if manager is not None:
            self._forget(manager, list((getattr(self, 'publishing', None) or {}).get('ids', ())))
            if getattr(self, 'fetching', None) is not None:
                self._forget(manager, [self.fetching[0]])
            if getattr(self, 'catalog_pending', None) is not None:
                self._forget(manager, [self.catalog_pending[0]])
        self.publishing = None
        self.fetching = None
        self.cooldown = 0
        self.catalog_pending = None
        self.catalog_loaded = False
        self.catalog_enabled = False
        self.fetch_cooldown = 0
        self.dirty = 'resync'
        self.own = None
        self.refused_hash = None
        self.stale = 0
        self.unsupported = False
        self.wanted = {}
        self.remote = {}
        self.fetched = set()
        self.announced = {}
        self.ticks = 0
        self.online = False
        self.read_error = None
        self.read_stats = {}
        self.objects_only = False

    def mark_dirty(self, reason='object_event'):
        """Événement natif : le terrain sera relu au prochain relevé. Aucune lecture ici."""
        if self.dirty is None or reason == 'build_exit':
            self.dirty = reason

    @staticmethod
    def _forget(manager, ids):
        forget = getattr(manager, 'forget', None)
        for request_id in ids if forget is not None else ():
            try:
                forget(request_id)
            except Exception as error:
                log('terrain', type(error).__name__)

    def _reject(self, reason):
        self.rejected[reason] = self.rejected.get(reason, 0) + 1
        log('terrain', 'rejected:' + reason)

    def tick(self, manager, authenticated, character, snapshot, allowed=None):
        if not authenticated:
            if self.online:
                self.reset()
            return None
        self.online = True
        if not isinstance(character, dict) or character.get('status') != 'bound' or self.unsupported:
            return None
        self.ticks += 1
        self.objects_only = isinstance(snapshot, dict) and snapshot.get('terrainObjectsOnly') is True
        self._expire(manager)
        try:
            local = self.locate()
        except Exception as error:
            log('terrain_locate', type(error).__name__)
            local = None
        if not isinstance(local, dict):
            local = None
        household = character.get('householdId')
        own = own_lot(snapshot, household)
        sent = self._publish(manager, character, own, local)
        self._want(snapshot, household)
        self.catalog_enabled = isinstance(snapshot, dict) and snapshot.get('terrainCatalog') is True
        self._fetch(manager, local)
        self._apply(local, own, allowed)
        return sent

    def flush(self, manager, authenticated, character, snapshot, allowed=None):
        """Sauvegarde du jeu : point de contrôle immédiat, sans attendre le délai de nouvel essai."""
        self.cooldown = 0
        if self.dirty is None:
            self.dirty = 'checkpoint'
        return self.tick(manager, authenticated, character, snapshot, allowed)

    def _expire(self, manager):
        """Requête restée sans réponse : abandonnée, jamais rejouée telle quelle."""
        flight = self.publishing
        if flight is not None and self.ticks - flight['tick'] > FLIGHT_TICKS:
            self._forget(manager, list(flight['ids']))
            self.publishing, self.dirty, self.cooldown = None, 'resync', RETRY_TICKS
        if self.fetching is not None and self.ticks - self.fetching[3] > FLIGHT_TICKS:
            self._forget(manager, [self.fetching[0]])
            self.fetching, self.fetch_cooldown = None, RETRY_TICKS
        if self.catalog_pending is not None and self.ticks - self.catalog_pending[1] > FLIGHT_TICKS:
            self._forget(manager, [self.catalog_pending[0]])
            self.catalog_pending = None
            self.catalog_loaded = True

    # --- Notre terrain : publication -------------------------------------------------------------------------

    def _publish(self, manager, character, own, local):
        binding = character.get('binding')
        native = binding.get('gameHouseholdId') if isinstance(binding, dict) else None
        if (local is None or own is None or local.get('lotId') != own or not isinstance(native, str)
                or local.get('ownerHouseholdId') != native):
            # Pas chez nous, ou foyer propriétaire natif inattendu : rien n'est lu ni publié.
            return None
        if self.own is None or self.own['lotId'] != own:
            self.own = {'lotId': own, 'revision': 0, 'hash': None}
            self.dirty, self.refused_hash, self.stale = 'resync', None, 0
        if self.dirty is None and self.ticks % CHECKPOINT_TICKS == 0:
            self.dirty = 'checkpoint'
        if self.dirty is None or self.publishing is not None:
            return None
        if self.cooldown > 0:
            self.cooldown -= 1
            return None
        snapshot = self._snapshot(own)
        if snapshot is None:
            self.cooldown = RETRY_TICKS
            return None
        snapshot_hash = terrain.digest(snapshot)
        reason, self.dirty = self.dirty, None
        if snapshot_hash in (self.own['hash'], self.refused_hash):
            return None
        payloads = terrain.pages(snapshot, snapshot_hash, self.new_id(), self.own['revision'], reason)
        ids = [self.new_id() for _ in payloads]
        try:
            for request_id, payload in zip(ids, payloads):
                manager.request('terrain.publish', payload, request_id)
        except (ValueError, RuntimeError) as error:
            # Type hors liste blanche, file pleine ou page trop grande : aucun transfert partiel n'est suivi.
            log('terrain_publish', type(error).__name__)
            self._forget(manager, ids)
            self.dirty, self.cooldown = reason, RETRY_TICKS
            return None
        self.publishing = {'ids': set(ids), 'final': ids[-1], 'hash': snapshot_hash, 'lotId': own, 'tick': self.ticks}
        return 'terrain.publish'

    def _snapshot(self, lot_id):
        self.read_stats = {}
        try:
            content = self.read()
        except Exception as error:
            self.read_error = type(error).__name__
            log('terrain_read', self.read_error)
            return None
        if not isinstance(content, dict) or 'error' in content:
            self.read_error = content.get('error') if isinstance(content, dict) else 'unreadable'
            log('terrain_read', str(self.read_error))
            return None
        self.read_stats = {'skipped': content.get('skipped'), 'blocksReadable': content.get('blocksReadable'),
                           'objectErrors': dict(content.get('readErrors') or {}), 'blockError': content.get('blockError')}
        if content.get('skipped') != 0 or (content.get('blocksReadable') is not True and not self.objects_only):
            self.read_error = 'partial_read'
            return None  # Une absence de lecture n'est pas une suppression d'objet ou de pièce.
        if not content.get('objects') and not content.get('blocks'):
            # Terrain sans objet ni contour : lot pas encore chargé, jamais une maison vide imposée aux autres.
            self.read_error = 'empty_read'
            return None
        try:
            candidate = terrain.build_snapshot(lot_id, content['objects'], content['blocks'])
            if content.get('blocksReadable') is not True:
                candidate.update(schema=2, blocks=[])
            snapshot = terrain.validate_snapshot(candidate)
        except (KeyError, TypeError):
            snapshot = None
        self.read_error = None if snapshot is not None else 'invalid'
        return snapshot

    def _published(self, manager, message, request_id):
        flight = self.publishing
        if message.get('ok') is not True:
            code = _code(message)
            log('terrain_publish', code)
            self.publishing = None
            self._forget(manager, [i for i in flight['ids'] if i != request_id])
            if code == 'unknown_type':
                self.unsupported = True
            elif code == 'stale_revision':
                self._stale(message.get('payload'), flight)
            elif code in ('storage_unavailable', 'request_limit', 'error', 'ack_timeout'):
                self.dirty, self.cooldown = 'resync', RETRY_TICKS
            else:
                # forbidden, build_limit, invalid_request… : pas de nouvel essai tant que le contenu n'a pas changé.
                self.refused_hash = flight['hash']
            return
        flight['ids'].discard(request_id)
        if request_id != flight['final']:
            return
        self.publishing = None
        payload = message.get('payload')
        revision = _revision(payload.get('revision')) if isinstance(payload, dict) else None
        if (revision is None or self.own is None or self.own['lotId'] != flight['lotId']
                or payload.get('lotId') != flight['lotId'] or payload.get('hash') != flight['hash']
                or revision < self.own['revision']):
            # ACK d'un autre terrain, d'une autre empreinte ou d'une révision qui recule : rien n'est tenu pour acquis.
            self._reject('ack')
            self.dirty, self.cooldown = 'resync', RETRY_TICKS
            return
        self.own['revision'], self.own['hash'], self.stale = revision, flight['hash'], 0
        log('terrain_publish', 'ok')

    def _stale(self, payload, flight):
        current = _revision(payload.get('currentRevision')) if isinstance(payload, dict) else None
        self.stale += 1
        if self.own is None or self.stale >= MAX_STALE:
            self.refused_hash = flight['hash']
            return
        self.dirty = 'resync'
        if current is not None and current > self.own['revision']:
            # Le propriétaire fait foi sur son terrain : il repart de la révision du serveur.
            self.own['revision'] = current
        else:
            self.cooldown = RETRY_TICKS

    # --- Terrains des autres : réception ---------------------------------------------------------------------

    def _want(self, snapshot, household):
        self.wanted = {home['lotId']: home['playerId'] for home in remote_homes(snapshot, household)[:MAX_REMOTE]}
        for lot_id in list(self.remote):
            if self.wanted.get(lot_id) != self.remote[lot_id]['ownerId']:
                # Terrain libéré ou passé à un autre joueur : l'ancien contenu n'est plus authentifié.
                del self.remote[lot_id]
                self.fetched.discard(lot_id)
        self.fetched.intersection_update(self.wanted)
        for lot_id in [lot_id for lot_id in self.announced if lot_id not in self.wanted]:
            del self.announced[lot_id]

    def _announce(self, payload):
        if not isinstance(payload, dict):
            return self._reject('invalid')
        lot_id, revision = payload.get('lotId'), _revision(payload.get('revision'))
        if lot_id not in self.wanted:
            return None
        if revision is None or not isinstance(payload.get('hash'), str) or not _HASH.fullmatch(payload['hash']):
            return self._reject('invalid')
        if payload.get('ownerId') != self.wanted[lot_id]:
            return self._reject('owner_mismatch')
        known = self.remote.get(lot_id)
        if known is not None and revision <= known['revision']:
            return self._reject('stale')
        self.announced[lot_id] = max(revision, self.announced.get(lot_id, 0))
        return None

    def _fetch(self, manager, local):
        if self.catalog_enabled and not self.catalog_loaded and self.wanted:
            if self.catalog_pending is None:
                request_id = self.new_id()
                try:
                    manager.request('terrain.catalog', {}, request_id)
                    self.catalog_pending = (request_id, self.ticks)
                except (ValueError, RuntimeError) as error:
                    log('terrain_catalog', type(error).__name__)
            return
        if self.fetching is not None:
            return
        if self.fetch_cooldown > 0:
            self.fetch_cooldown -= 1
            return
        here = local.get('lotId') if local is not None else None
        if self.ticks % REFRESH_TICKS == 0:
            self.fetched.clear()
        elif self.ticks % REFRESH_LOCAL_TICKS == 0:
            # Le terrain où l'on se trouve est revérifié souvent : une annonce perdue ne coûte que ce délai.
            self.fetched.discard(here)
        for lot_id in sorted(self.wanted, key=lambda lot: (lot != here, lot)):
            known = self.remote.get(lot_id)
            if lot_id in self.fetched and lot_id not in self.announced:
                continue
            payload = {'lotId': lot_id}
            if known is not None:
                payload['knownRevision'], payload['knownHash'] = known['revision'], known['hash']
            request_id = self.new_id()
            try:
                manager.request('terrain.get', payload, request_id)
            except (ValueError, RuntimeError) as error:
                log('terrain_get', type(error).__name__)
                self.fetch_cooldown = RETRY_TICKS
                return
            self.fetching = (request_id, lot_id, self.wanted[lot_id], self.ticks)
            return

    def _fetched(self, message):
        _, lot_id, owner, _ = self.fetching
        self.fetching = None
        self.fetched.add(lot_id)
        if message.get('ok') is not True:
            code = _code(message)
            if code == 'unknown_type':
                self.unsupported = True
            elif code != 'not_found':
                log('terrain_get', code)
                self.fetch_cooldown = RETRY_TICKS
            return None
        payload = message.get('payload')
        if not isinstance(payload, dict):
            return self._reject('invalid')
        if payload.get('lotId') != lot_id:
            return self._reject('lot_mismatch')
        if self.wanted.get(lot_id) != owner or payload.get('ownerId') != owner:
            return self._reject('owner_mismatch')
        revision, snapshot_hash = _revision(payload.get('revision')), payload.get('hash')
        if revision is None or not isinstance(snapshot_hash, str) or not _HASH.fullmatch(snapshot_hash):
            return self._reject('invalid')
        if revision >= self.announced.get(lot_id, 0):
            self.announced.pop(lot_id, None)
        known = self.remote.get(lot_id)
        if payload.get('unchanged') is True:
            if known is None or (known['revision'], known['hash']) != (revision, snapshot_hash):
                # « Inchangé » pour un état que nous n'avons pas : l'état local est abandonné et redemandé en entier.
                self.remote.pop(lot_id, None)
                self.fetched.discard(lot_id)
                self.fetch_cooldown = RETRY_TICKS
                return self._reject('conflict')
            return None
        if known is not None and revision < known['revision']:
            return self._reject('stale')
        if known is not None and revision == known['revision']:
            return None if snapshot_hash == known['hash'] else self._reject('conflict')
        snapshot = terrain.validate_snapshot(payload.get('snapshot'))
        if snapshot is None:
            return self._reject('invalid')
        if snapshot['lotId'] != lot_id:
            return self._reject('lot_mismatch')
        if terrain.digest(snapshot) != snapshot_hash:
            return self._reject('hash_mismatch')
        self.remote[lot_id] = {'ownerId': owner, 'revision': revision, 'hash': snapshot_hash, 'snapshot': snapshot,
                               'applied': False, 'blocked': None}
        log('terrain_get', 'ok')
        return None

    def on_messages(self, manager, messages):
        for message in messages if isinstance(messages, list) else []:
            if not isinstance(message, dict):
                continue
            request_id = message.get('id')
            if self.catalog_pending is not None and request_id == self.catalog_pending[0]:
                self.catalog_pending = None
                self.catalog_loaded = True
                payload = message.get('payload')
                entries = payload.get('terrains') if isinstance(payload, dict) else None
                if message.get('ok') is not True or not isinstance(entries, list) or len(entries) > 64:
                    self._reject('catalog')
                    continue
                self.fetched.update(self.wanted)
                for entry in entries:
                    lot = entry.get('lotId') if isinstance(entry, dict) else None
                    if isinstance(lot, str) and lot in self.wanted:
                        self.fetching = (request_id, lot, self.wanted[lot], self.ticks)
                        self._fetched({'ok':True, 'payload':entry})
            elif self.publishing is not None and request_id in self.publishing['ids']:
                self._published(manager, message, request_id)
            elif self.fetching is not None and request_id == self.fetching[0]:
                self._fetched(message)
            elif message.get('type') == 'terrain.updated' and 'ok' not in message:
                self._announce(message.get('payload'))

    # --- Application : seulement avec un applicateur injecté --------------------------------------------------

    def _apply(self, local, own, allowed):
        if self.apply is None or local is None or local.get('buildBuy') is not False:
            return
        lot_id = local.get('lotId')
        entry = self.remote.get(lot_id)
        if entry is None or lot_id == own or entry['applied'] or entry['blocked'] not in (None, 'ownership', 'local_read', 'apply_failed'):
            return
        # Après trois échecs, attendre avant une nouvelle série avec relecture et ACL.
        if self.ticks < entry.get('retryAt', 0):
            return
        if entry.get('attempts', 0) >= MAX_APPLY_ATTEMPTS:
            entry['attempts'] = 0
        expected = allowed.get(lot_id) if isinstance(allowed, dict) else None
        if expected is None or local.get('ownerHouseholdId') not in expected:
            # Foyer propriétaire local inattendu : aucun setter natif sur le terrain d'un tiers.
            if entry['blocked'] != 'ownership':
                self._reject('ownership')
            entry['blocked'] = 'ownership'
            return
        if not entry['snapshot']['objects'] and not entry['snapshot']['blocks']:
            # Relevé distant vide (ancien client) : l'appliquer supprimerait tous les objets locaux du terrain.
            if entry['blocked'] != 'empty_snapshot':
                self._reject('empty_snapshot')
            entry['blocked'] = 'empty_snapshot'
            return
        unread = entry['blocked'] == 'local_read'
        entry['blocked'] = None  # Le foyer S4T peut être apparu après la réception du relevé.
        try:
            content = self.read()
            if (not isinstance(content, dict) or 'error' in content or content.get('skipped') != 0):
                # Terrain local illisible ou lu en partie : rien n'est écrit, nouvel essai au relevé suivant.
                if not unread:
                    self._reject('local_read')
                entry['blocked'] = 'local_read'
                return
            entry['attempts'] = entry.get('attempts', 0) + 1
            done = self.apply(local.get('zoneId'), terrain.plan(content['objects'], entry['snapshot']['objects']))
        except Exception as error:
            log('terrain_apply', type(error).__name__)
            done = False
        entry['applied'] = done is True
        if not entry['applied']:
            entry['blocked'] = 'apply_failed'
            entry['retryAt'] = self.ticks + (APPLY_RECOVERY_TICKS
                if entry.get('attempts', 0) >= MAX_APPLY_ATTEMPTS else RETRY_TICKS)

    def status(self):
        """Diagnostic sans contenu de terrain (compteurs seulement)."""
        if self.unsupported:
            state = 'unsupported'
        elif not self.online:
            state = 'offline'
        elif self.publishing is not None:
            state = 'publishing'
        else:
            state = 'idle'
        remote = {}
        for lot_id, entry in self.remote.items():
            if entry['applied']:
                progress = 'applied'
            else:
                progress = 'blocked:' + entry['blocked'] if entry['blocked'] else 'ready'
            remote[lot_id] = {'revision': entry['revision'], 'objects': len(entry['snapshot']['objects']),
                              'blocks': len(entry['snapshot']['blocks']), 'state': progress,
                              'geometryKnown': entry['snapshot']['schema'] == 1}
        own = self.own or {}
        return {'state': state, 'ownLot': own.get('lotId'), 'ownRevision': own.get('revision'),
                'published': own.get('hash') is not None, 'dirty': self.dirty, 'readError': self.read_error,
                'fetching': self.fetching[1] if self.fetching else None, 'remote': remote,
                'rejected': dict(self.rejected), 'applier': self.apply is not None,
                'readStats': dict(self.read_stats),
                'lastApply': _last_apply(self.apply),
                'objectsOnlySupported': self.objects_only,
                'architecture': 'not_transferred', 'gameReadiness': 'NOT_TESTED_GAME'}
