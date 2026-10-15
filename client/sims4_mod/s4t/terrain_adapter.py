"""Terrain seul : lecture native, format canonique, empreinte, pagination et plan de différences (13/10/2026).

API vérifiée par désassemblage de simulation.zip 1.128 (lecture seule), jamais exécutée en jeu :
services.current_zone(), Zone.id, Zone.lot, Lot.zone_owner_household_id, Zone.is_in_build_buy ;
services.get_persistence_service().get_zone_proto_buff(zone_id) : lot_description_id, active_plex ;
services.object_manager().valid_objects() ; par objet is_sim, is_on_active_lot(), definition.id, position,
orientation, routing_surface.secondary_id, scale, parent ;
build_buy.get_all_block_polygons(plex_id).values() : couples (polygones, niveau), comme les lit
world/lot_geom_utils.get_random_points_on_floor ; services.get_plex_service().get_active_zone_plex_id(),
plex.plex_enums.INVALID_PLEX_ID ;
build_buy.register_build_buy_exit_callback / unregister_build_buy_exit_callback ;
IndexedManager.register_callback(type, callback), indexed_manager.CallbackTypes.ON_OBJECT_ADD /
ON_OBJECT_REMOVE / ON_OBJECT_LOCATION_CHANGED.

Ce module ne crée, ne déplace ni ne supprime rien dans le jeu. Murs, sols, toits et relief sont un blob natif
(ZoneObjectData.architecture_data) sans accès Python : seuls les objets et les contours de pièces sont lus.
Aucun identifiant de Sim, de foyer ou d'objet, aucun coût ni état ne sort d'ici. NOT_TESTED_GAME.
"""
import collections
import hashlib
import json
import math
import re
import threading

from .journal import log

SCHEMA = 1
OBJECT_FIELDS = 8
MAX_OBJECTS = 1000
MAX_BLOCKS = 256
MAX_VERTICES = 2000
MIN_POLYGON, MAX_POLYGON = 3, 64
PAGE_OBJECTS = 250
MAX_PAGE_BYTES = 49152
MAX_COORDINATE = 1000000.0
MAX_MM = 10 ** 9
MAX_ROTATION = 6283
MIN_LEVEL, MAX_LEVEL = -8, 16
MAX_SCALE = 100000
_NATIVE = re.compile(r'^[1-9][0-9]{0,19}$')
_KEYS = {'schema', 'lotId', 'objects', 'blocks'}


def _id(value):
    """Identifiant natif en chaîne décimale exacte ; zéro, booléen, flottant et chaîne refusés."""
    return str(value) if type(value) is int and 0 < value < 10 ** 20 else None


def _mm(value):
    if type(value) not in (int, float) or not math.isfinite(value) or abs(value) > MAX_COORDINATE:
        return None
    return int(round(value * 1000))


def _whole(value, low, high):
    return type(value) is int and low <= value <= high


def _valid_row(row):
    return (type(row) is list and len(row) == OBJECT_FIELDS and isinstance(row[0], str)
            and _NATIVE.fullmatch(row[0]) is not None and all(_whole(v, -MAX_MM, MAX_MM) for v in row[1:4])
            and _whole(row[4], 0, MAX_ROTATION) and _whole(row[5], MIN_LEVEL, MAX_LEVEL)
            and _whole(row[6], 1, MAX_SCALE) and _whole(row[7], 0, 1))


def _valid_block(block):
    if type(block) is not list or len(block) != 2 or not _whole(block[0], MIN_LEVEL, MAX_LEVEL):
        return False
    points = block[1]
    return (type(points) is list and MIN_POLYGON <= len(points) <= MAX_POLYGON
            and all(type(p) is list and len(p) == 2 and all(_whole(v, -MAX_MM, MAX_MM) for v in p) for p in points))


def build_snapshot(lot_id, objects, blocks):
    """Snapshot canonique : lignes et contours triés, sans rien d'autre."""
    return {'schema': SCHEMA, 'lotId': lot_id, 'objects': sorted(objects), 'blocks': sorted(blocks)}


def validate_snapshot(value):
    """Le snapshot s'il respecte exactement le format canonique (bornes, types, tri), sinon None."""
    if not isinstance(value, dict) or set(value) != _KEYS or type(value['schema']) is not int or value['schema'] != SCHEMA:
        return None
    lot_id, objects, blocks = value['lotId'], value['objects'], value['blocks']
    if not isinstance(lot_id, str) or _NATIVE.fullmatch(lot_id) is None:
        return None
    if type(objects) is not list or len(objects) > MAX_OBJECTS or not all(_valid_row(row) for row in objects):
        return None
    if type(blocks) is not list or len(blocks) > MAX_BLOCKS or not all(_valid_block(block) for block in blocks):
        return None
    if sum(len(block[1]) for block in blocks) > MAX_VERTICES or objects != sorted(objects) or blocks != sorted(blocks):
        return None
    return value


def digest(snapshot):
    """SHA-256 du JSON à clés triées sans espace : entiers et chaînes de chiffres, recalculable en C#."""
    text = json.dumps(snapshot, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(text.encode('ascii')).hexdigest()


def pages(snapshot, snapshot_hash, transfer_id, base_revision, reason):
    """Charges utiles terrain.publish : contours sur une page à eux seuls, objets par pages bornées."""
    objects, blocks = snapshot['objects'], snapshot['blocks']
    parts = [([], blocks)] if blocks else []
    parts.extend((objects[start:start + PAGE_OBJECTS], []) for start in range(0, len(objects), PAGE_OBJECTS))
    if not parts:
        parts.append(([], []))
    return [{'lotId': snapshot['lotId'], 'baseRevision': base_revision, 'hash': snapshot_hash,
             'transferId': transfer_id, 'page': index, 'pages': len(parts), 'count': len(objects),
             'blockCount': len(blocks), 'reason': reason, 'objects': rows, 'blocks': outlines}
            for index, (rows, outlines) in enumerate(parts)]


def plan(current, wanted):
    """Différence en multi-ensemble entre les lignes locales et les lignes voulues ; rien n'est appliqué ici."""
    have = collections.Counter(tuple(row) for row in current)
    want = collections.Counter(tuple(row) for row in wanted)
    return {'create': sorted(list(row) for row in (want - have).elements()),
            'remove': sorted(list(row) for row in (have - want).elements())}


class TerrainReader:
    """Lecture seule sur le thread principal ; une valeur douteuse écarte l'objet, elle n'est jamais devinée."""

    def __init__(self, services, build_buy=None, yaw=None, invalid_plex=0):
        self.services = services
        self.build_buy = build_buy
        self.yaw = yaw
        self.invalid_plex = invalid_plex
        self.thread_id = threading.get_ident()

    def _main_thread(self):
        if threading.get_ident() != self.thread_id:
            raise RuntimeError('Lecture du jeu interdite hors thread principal')

    def locate(self):
        """{lotId, zoneId, ownerHouseholdId, buildBuy} du terrain chargé, None s'il n'est pas identifiable."""
        self._main_thread()
        try:
            zone = self.services.current_zone()
            if zone is None:
                return None
            proto = self.services.get_persistence_service().get_zone_proto_buff(zone.id)
            if proto is None or getattr(proto, 'active_plex', 0):
                # Appartement : plusieurs logements partagent la même description.
                return None
            lot_id, zone_id = _id(proto.lot_description_id), _id(zone.id)
            if lot_id is None or zone_id is None:
                return None
            building = zone.is_in_build_buy
            if callable(building):
                building = building()
            return {'lotId': lot_id, 'zoneId': zone_id, 'ownerHouseholdId': _id(zone.lot.zone_owner_household_id),
                    'buildBuy': bool(building)}
        except Exception as error:
            log('terrain_locate', type(error).__name__)
            return None

    def read(self):
        """{objects, blocks, blocksReadable, skipped} du terrain actif, ou {error} s'il dépasse les bornes."""
        self._main_thread()
        rows, skipped, errors = [], 0, collections.Counter()
        for item in self.services.object_manager().valid_objects():
            try:
                row = self._row(item)
            except Exception as error:
                row = False
                errors['object_' + type(error).__name__] += 1
            if row is None:
                continue
            if row is False:
                skipped += 1
                errors['invalid_object'] += 1
                continue
            rows.append(row)
            if len(rows) > MAX_OBJECTS:
                return {'error': 'too_many_objects'}
        blocks = self._blocks()
        return {'objects': sorted(rows), 'blocks': blocks or [], 'blocksReadable': blocks is not None,
                'skipped': skipped, 'readErrors': dict(errors), 'blockError': self.block_error}

    def _row(self, item):
        """Ligne de l'objet ; None s'il n'appartient pas au terrain (Sim, porté, dehors) ; False s'il est illisible."""
        if getattr(item, 'is_sim', True) or not item.is_on_active_lot():
            return None
        parent = item.parent
        if parent is not None and getattr(parent, 'is_sim', True):
            return None
        position = item.position
        rotation = self.yaw(item.orientation)
        scale = item.scale
        if type(rotation) not in (int, float) or not math.isfinite(rotation):
            return False
        if type(scale) not in (int, float) or not math.isfinite(scale):
            return False
        row = [_id(item.definition.id), _mm(position.x), _mm(position.y), _mm(position.z),
               int(round((rotation % (2 * math.pi)) * 1000)), item.routing_surface.secondary_id,
               int(round(scale * 1000)), 0 if parent is None else 1]
        return row if None not in row and _valid_row(row) else False

    def _blocks(self):
        """Contours de pièces triés, [] sans module build_buy, None s'ils sont illisibles ou hors bornes."""
        self.block_error = None
        if self.build_buy is None:
            return []
        try:
            plex_id = self.services.get_plex_service().get_active_zone_plex_id() or self.invalid_plex
            blocks, vertices = [], 0
            for polygons, level in self.build_buy.get_all_block_polygons(plex_id).values():
                for polygon in polygons:
                    block = [level, [[_mm(point.x), _mm(point.z)] for point in polygon]]
                    vertices += len(block[1])
                    if not _valid_block(block) or len(blocks) >= MAX_BLOCKS or vertices > MAX_VERTICES:
                        self.block_error = 'invalid_or_oversized_polygon'
                        return None
                    blocks.append(block)
            return sorted(blocks)
        except Exception as error:
            self.block_error = type(error).__name__
            log('terrain_blocks', type(error).__name__)
            return None


class TerrainEvents:
    """Événements natifs traduits en sink(raison) ; aucune lecture ni écriture du jeu dans les callbacks."""

    def __init__(self, sink, build_buy, object_manager, callback_types):
        self.sink = sink
        self.build_buy = build_buy
        self.object_manager = object_manager
        self.callback_types = callback_types
        self.exit_registered = False
        self.manager = None
        self.registered = []

    def _stop_objects(self):
        for kind in list(self.registered):
            try:
                self.manager.unregister_callback(kind, self._object)
                self.registered.remove(kind)
            except Exception as error:
                log('terrain_events', type(error).__name__)
        if not self.registered:
            self.manager = None
        return not self.registered

    def start(self):
        """À chaque zone chargée : le gestionnaire d'objets est neuf, la liste build/buy est globale."""
        complete = True
        if not self.exit_registered:
            try:
                self.build_buy.register_build_buy_exit_callback(self._exit)
                self.exit_registered = True
            except Exception as error:
                log('terrain_events', type(error).__name__)
                complete = False
        try:
            manager, kinds = self.object_manager(), self.callback_types
            if manager is not self.manager:
                if not self._stop_objects():
                    return False
                self.manager = manager
            for kind in (kinds.ON_OBJECT_ADD, kinds.ON_OBJECT_REMOVE, kinds.ON_OBJECT_LOCATION_CHANGED):
                if kind not in self.registered:
                    manager.register_callback(kind, self._object)
                    self.registered.append(kind)
        except Exception as error:
            log('terrain_events', type(error).__name__)
            self._stop_objects()
            complete = False
        return complete

    def stop(self):
        self._stop_objects()
        if not self.exit_registered:
            return
        try:
            self.build_buy.unregister_build_buy_exit_callback(self._exit)
            self.exit_registered = False
        except Exception as error:
            log('terrain_events', type(error).__name__)

    def _signal(self, reason):
        # Une exception ne doit jamais remonter dans la boucle du jeu.
        try:
            self.sink(reason)
        except Exception as error:
            log('terrain_events', type(error).__name__)

    def _exit(self, *_args, **_kwargs):
        self._signal('build_exit')

    def _object(self, item=None, *_args, **_kwargs):
        if getattr(item, 'is_sim', True):
            return
        self._signal('object_event')


def game_reader():
    import build_buy
    import services
    from plex.plex_enums import INVALID_PLEX_ID
    from sims4.math import yaw_quaternion_to_angle
    return TerrainReader(services, build_buy, yaw_quaternion_to_angle, INVALID_PLEX_ID)


def game_events(sink):
    import build_buy
    import indexed_manager
    import services
    return TerrainEvents(sink, build_buy, services.object_manager, indexed_manager.CallbackTypes)
