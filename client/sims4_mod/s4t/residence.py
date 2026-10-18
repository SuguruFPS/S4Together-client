"""Résidences canoniques : préserver les PNJ non joués avant de libérer leur adresse.

Uniquement appelé pour une partie multijoueur liée. Aucun Sim supprimé, aucun budget modifié.
Le journal précède toute écriture native et reste disponible après panne. NOT_TESTED_GAME.
"""
import functools
import json
import os
import uuid
from pathlib import Path
from .journal import log


def _unplayed_npc(household, active_id):
    """Sims d'un foyer PNJ jamais joué, non actif, non S4T et sans Sim sélectionnable ; None sinon."""
    if (household is None or household.id == active_id
            or household.is_player_household is not False or household.is_played_household is not False):
        return None
    if type(household.description) is not str or type(household.name) is not str:
        return None
    if household.description.strip().upper().startswith('S4T:') or '(S4T)' in household.name.upper():
        return None
    infos = tuple(household.sim_info_gen())
    if not 0 < len(infos) <= 8 or any(info.is_selectable is not False for info in infos):
        return None
    return infos


def replaceable(household, active_id, zone_id):
    """Un PNJ non joué, non sélectionnable et non instancié, dont le domicile est certain."""
    try:
        if household is None or household.home_zone_id != zone_id:
            return False
        infos = _unplayed_npc(household, active_id)
        return infos is not None and all(info.is_instanced(allow_hidden_flags=0xFFFFFFFF) is False
                                         for info in infos)
    except Exception:
        return False


def _restorable(household, active_id):
    """Rollback permis seulement vers un PNJ encore non joué ; l'instanciation n'empêche pas de lui rendre sa maison."""
    try:
        return _unplayed_npc(household, active_id) is not None
    except Exception:
        return False


def _active_household_id():
    try:
        import services
        return services.active_household_id()
    except Exception:
        return None


def _native_set_home(household, zone_id):
    """Méthode native d'origine, sans ResidenceGuard ; jamais d'écriture directe dans le proto de zone."""
    method = household.set_household_lot_ownership
    original = getattr(method, 's4t_original', None)
    if original is None:
        return method(zone_id=zone_id)
    return original(household, zone_id=zone_id)


class ResidenceJournal:
    """Petit journal atomique par sauvegarde/foyer ; aucune écriture dans un fichier .save."""

    def __init__(self, path, context):
        self.path, self.context = Path(path), context
        self.records()  # Une identité de sauvegarde différente ou un journal abîmé bloque les mutations.

    def records(self):
        if not self.path.exists():
            return []
        if self.path.is_symlink() or self.path.stat().st_size > 65536:
            raise ValueError('residence_journal_size')
        value = json.loads(self.path.read_text(encoding='utf-8'))
        if (type(value) is not dict or set(value) != {'schema', 'context', 'records'}
                or type(value['schema']) is not int or value['schema'] != 1
                or value['context'] != self.context or type(value['records']) is not list
                or len(value['records']) > 64):
            raise ValueError('residence_journal_context')
        seen = set()
        for record in value['records']:
            if (type(record) is not dict or set(record) != {'npcHouseholdId','zoneId','playerId','proxyHouseholdId'}
                    or any(type(record[k]) is not int or not 0 < record[k] < 2**64
                           for k in ('npcHouseholdId','zoneId','proxyHouseholdId'))
                    or type(record['playerId']) is not str or not 0 < len(record['playerId']) <= 100
                    or record['npcHouseholdId'] in seen):
                raise ValueError('residence_journal_record')
            seen.add(record['npcHouseholdId'])
        return value['records']

    def _write(self, records):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = json.dumps({'schema':1,'context':self.context,'records':records},sort_keys=True).encode('utf-8')
        if len(data) > 65536 or len(records) > 64:
            raise ValueError('residence_journal_full')
        temp = self.path.with_name(self.path.name + '.' + uuid.uuid4().hex + '.tmp')
        try:
            with temp.open('xb') as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            temp.replace(self.path)
        finally:
            if temp.exists():
                temp.unlink()

    def displace(self, household, zone_id, player_id, proxy_id, install, occupant=None, active_id=None):
        previous = self.records()
        if any(r['npcHouseholdId'] == household.id for r in previous):
            raise RuntimeError('residence_already_backed_up')
        record = {'npcHouseholdId':household.id,'zoneId':zone_id,'playerId':player_id,'proxyHouseholdId':proxy_id}
        self._write(previous + [record])
        try:
            household.set_household_lot_ownership(zone_id=0)
            household.save_data()
            # Libération relue avant toute installation : un terrain encore attribué au PNJ n'est jamais pris.
            if household.home_zone_id != 0 or (occupant is not None and occupant(zone_id) != 0):
                raise RuntimeError('residence_release_unconfirmed')
            return install()
        except Exception:
            self._rollback(household, zone_id, previous, occupant,
                           _active_household_id() if active_id is None else active_id)
            raise

    def _rollback(self, household, zone_id, previous, occupant, active_id):
        """Rend son terrain au PNJ journalisé ; le journal ne tombe que si domicile et occupant le confirment.

        Sans lecture d'occupant (appel pur), seul home_zone_id confirme. Toute erreur garde le journal.
        """
        try:
            if occupant is not None and active_id is None:
                log('residence_rollback', 'active_household_unknown')
                return
            if occupant is not None and occupant(zone_id) not in (0, household.id):
                # La copie ou un autre foyer occupe déjà le terrain : jamais écrasé.
                log('residence_rollback', 'occupied')
                return
            if household.home_zone_id not in (0, zone_id):
                log('residence_rollback', 'moved')
                return
            if not _restorable(household, active_id):
                log('residence_rollback', 'protected')
                return
            if household.home_zone_id != zone_id:
                # La garde bloque tout PNJ sur une résidence réservée ; seul ce retour journalisé la contourne.
                _native_set_home(household, zone_id)
                household.save_data()
            if household.home_zone_id == zone_id and (occupant is None or occupant(zone_id) == household.id):
                self._write(previous)
            else:
                log('residence_rollback', 'unconfirmed')
        except Exception as error:
            log('residence_rollback', type(error).__name__)

    def restore(self, reserved, lookup, occupant, active_id):
        records = self.records()
        for record in tuple(records):
            zone_id = record['zoneId']
            if zone_id in reserved:
                continue
            household = lookup(record['npcHouseholdId'])
            owner = occupant(zone_id)
            if household is None:
                continue
            if household.home_zone_id == zone_id and owner == household.id:
                records.remove(record)
                self._write(records)
            elif owner == 0 and replaceable(household, active_id, 0):
                household.set_household_lot_ownership(zone_id=zone_id)
                household.save_data()
                records.remove(record)
                self._write(records)


class ResidenceGuard:
    """Bloque les emménagements PNJ sur une résidence serveur et les déménagements autonomes de copies."""

    def __init__(self, active_household_id=lambda: None):
        self.homes = {}
        self.enabled = False
        self.active_household_id = active_household_id

    def set_homes(self, homes):
        self.homes, self.enabled = dict(homes), True

    def clear(self):
        self.homes, self.enabled = {}, False

    def wrap(self, original):
        @functools.wraps(original)
        def wrapped(household, zone_id=0, *args, **kwargs):
            # Inactif pendant les transitions : ne même pas lire les propriétés natives.
            if not self.enabled:
                return original(household, zone_id, *args, **kwargs)
            try:
                active_id = self.active_household_id()
            except Exception:
                active_id = None
            if active_id is not None and getattr(household, 'id', None) == active_id:
                return original(household, zone_id, *args, **kwargs)
            from .remote_household import parse_description
            parsed = parse_description(getattr(household, 'description', None))
            if self.enabled:
                expected = self.homes.get(parsed[0]) if parsed else None
                if parsed and (expected is None or zone_id != expected):
                    log('residence_guard_block', 'remote_move')
                    return None
                if zone_id in self.homes.values() and (not parsed or expected != zone_id):
                    log('residence_guard_block', 'reserved_home')
                    return None
            return original(household, zone_id, *args, **kwargs)
        wrapped.s4t_residence_guard = self
        wrapped.s4t_original = original
        return wrapped
