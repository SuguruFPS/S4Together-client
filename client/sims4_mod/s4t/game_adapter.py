"""Lecture des services sur le thread principal uniquement ; aucun setter du jeu.

API de présence vérifiée par désassemblage (1.128, lecture seule), jamais exécutée en jeu :
simulation.zip — services.get_persistence_service() ; PersistenceService.get_save_slot_proto_guid()
(_save_game_data_proto.guid ou None), get_save_slot_proto_buff() (save_slot ou None),
get_world_id_from_zone(zone_id) (0 si zone inconnue) ; Zone.lot.lot_id (lu par Zone.get_zone_info) ;
services.active_household(), active_household_id(), active_sim_info() ; Household.sim_info_gen() ;
SimInfo.is_instanced(), SimInfo.get_sim_instance() ; ClientObjectMixin.position (transform.translation)
et .orientation (transform.orientation). core.zip — sims4.math.yaw_quaternion_to_angle(quaternion).
"""
import math
import re
import threading

MAX_SIMS = 8
MAX_COORDINATE = 1000000.0
MIN_LEVEL, MAX_LEVEL = -8, 16
_POSTURE = re.compile(r'^[A-Za-z0-9_.\-]{1,64}$')


def _native(value):
    """Identifiant natif en chaîne décimale exacte ; zéro, booléen et flottant refusés."""
    return str(value) if type(value) is int and 0 < value < 10 ** 20 else None


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def game_yaw(orientation):
    from sims4.math import yaw_quaternion_to_angle
    return yaw_quaternion_to_angle(orientation)


class GameAdapter:
    def __init__(self, services=None, yaw=game_yaw):
        self.yaw = yaw
        self.thread_id = threading.get_ident()
        if services is None:
            try:
                import services as game_services
            except ImportError:
                game_services = None
            services = game_services
        self.services = services
        self.previous = None

    def _main_thread(self):
        if threading.get_ident() != self.thread_id:
            raise RuntimeError('Lecture du jeu interdite hors thread principal')

    def observe(self):
        self._main_thread()
        result = {'kind': 's4t.observation', 'loaded': False, 'zoneId': None, 'activeSimId': None,
                  'hookMode': 'poll', 'gameReadiness': 'NOT_TESTED_GAME'}
        if self.services is not None:
            try:
                zone = self.services.current_zone()
                sim = self.services.get_active_sim()
                if zone is not None:
                    result['zoneId'] = _native(getattr(zone, 'id', None))
                if sim is not None:
                    result['activeSimId'] = _native(getattr(sim, 'id', None))
                result['loaded'] = result['zoneId'] is not None and result['activeSimId'] is not None
            except (AttributeError, TypeError, RuntimeError):
                result['readError'] = 'services_unavailable'
        transition = 'unchanged'
        if self.previous is None:
            transition = 'loaded' if result['loaded'] else 'unloaded'
        elif self.previous['loaded'] != result['loaded']:
            transition = 'loaded' if result['loaded'] else 'unloaded'
        elif self.previous['zoneId'] != result['zoneId']:
            transition = 'zone_changed'
        elif self.previous['activeSimId'] != result['activeSimId']:
            transition = 'active_sim_changed'
        self.previous = dict(result)
        result['transition'] = transition
        return result

    def _read(self, missing, name, reader):
        """Une lecture native : valeur, ou None avec `name` ajouté aux identités indisponibles."""
        try:
            value = reader()
        except Exception:
            value = None
        if value is None:
            missing.append(name)
        return value

    def _save_id(self, persistence):
        guid = _native(persistence.get_save_slot_proto_guid())
        if guid is not None:
            return guid, 'save_slot_guid'
        slot = persistence.get_save_slot_proto_buff()
        slot_id = _native(getattr(slot, 'slot_id', None))
        # s4_guid_seed peut changer : jamais utilisé comme identité de sauvegarde.
        return (slot_id, 'save_slot_id') if slot_id is not None else None

    def presence(self):
        """Identités de la partie chargée pour game.join ; `unavailable` liste ce que le jeu n'a pas fourni.

        Rien n'est inventé : une identité absente reste None et interdit le join côté PresenceSync.
        """
        self._main_thread()
        result = {'saveId': None, 'saveIdSource': None, 'nativeHouseholdId': None, 'activeSimId': None,
                  'simIds': None, 'worldId': None, 'lotId': None}
        missing = []
        services = self.services
        if services is None:
            result['unavailable'] = ['services']
            return result
        zone = self._read(missing, 'zone', services.current_zone)
        persistence = self._read(missing, 'persistence', services.get_persistence_service)
        if persistence is not None:
            save = self._read(missing, 'saveId', lambda: self._save_id(persistence))
            if save is not None:
                result['saveId'], result['saveIdSource'] = save
        if zone is not None:
            result['lotId'] = self._read(missing, 'lotId', lambda: _native(zone.lot.lot_id))
            if persistence is not None:
                result['worldId'] = self._read(missing, 'worldId', lambda: _native(persistence.get_world_id_from_zone(zone.id)))
        household = self._read(missing, 'household', services.active_household)
        if household is not None:
            result['nativeHouseholdId'] = self._read(missing, 'nativeHouseholdId', lambda: _native(services.active_household_id()))
            def members():
                ids = [_native(info.sim_id) for info in household.sim_info_gen()]
                unique = len(set(ids)) == len(ids)
                return ids if ids and None not in ids and unique and len(ids) <= MAX_SIMS else None
            result['simIds'] = self._read(missing, 'simIds', members)
        result['activeSimId'] = self._read(missing, 'activeSimId', lambda: _native(services.active_sim_info().sim_id))
        result['unavailable'] = missing
        return result

    @staticmethod
    def _posture(sim):
        """'posture:<nom>' de la posture corporelle (Sim.posture, Posture.name), ou None. Ce n'est pas un clip."""
        try:
            name = getattr(sim.posture, 'name', None)
        except Exception:
            return None
        return 'posture:' + name if isinstance(name, str) and _POSTURE.match(name) else None

    @staticmethod
    def _level(sim):
        """Étage réel (Sim.routing_surface.secondary_id), ou None : jamais d'étage supposé."""
        try:
            level = sim.routing_surface.secondary_id
        except Exception:
            return None
        return level if type(level) is int and MIN_LEVEL <= level <= MAX_LEVEL else None

    def poses(self, sim_ids):
        """Poses réelles des Sims instanciés parmi `sim_ids`. `animation` = posture native observée, sinon absent."""
        self._main_thread()
        wanted = set(sim_ids)
        result = []
        try:
            household = self.services.active_household()
            infos = list(household.sim_info_gen()) if household is not None else []
        except Exception:
            return result
        for info in infos:
            try:
                sim_id = _native(info.sim_id)
                if sim_id not in wanted or not info.is_instanced():
                    continue
                sim = info.get_sim_instance()
                if sim is None:
                    continue
                position = sim.position
                values = (position.x, position.y, position.z, self.yaw(sim.orientation))
            except Exception:
                continue
            if all(_finite(v) for v in values) and all(abs(v) <= MAX_COORDINATE for v in values[:3]):
                x, y, z, rotation = (round(float(v), 3) for v in values)
                pose = {'simId': sim_id, 'position': {'x': x, 'y': y, 'z': z}, 'rotation': rotation}
                posture = self._posture(sim)
                if posture is not None:
                    pose['animation'] = posture
                level = self._level(sim)
                if level is not None:
                    pose['level'] = level
                result.append(pose)
                wanted.discard(sim_id)
        return result[:MAX_SIMS]
