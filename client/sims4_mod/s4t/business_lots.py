"""Commerces des joueurs (clinique vétérinaire, boutique, restaurant…) réservés sur le serveur (19/10/2026).

Un commerce acheté par un joueur est publié au serveur (`business.claim`) avec la description de terrain commune
(`lot_description_id`, voir home_lots.py). Les autres joueurs ne peuvent alors ni l'acheter (BusinessService.make_owner)
ni y emménager (ResidenceGuard) dans leur partie. La démolition native (bulldozer) n'est pas bloquée.

API vérifiée par lecture du bytecode simulation.zip 1.128 :
services.business_service().get_business_trackers_for_household(household_id) -> liste de BusinessTracker ;
BusinessTracker.business_type (BusinessType) et .business_managers ({zone_id: BusinessManager}) ;
BusinessService.make_owner(owner_household_id, business_type, zone_id, telemetry_context, from_load, business_data) ;
BusinessType : INVALID, RETAIL, RESTAURANT, VET, RENTAL_UNIT, SMALL_BUSINESS. Locations (RENTAL_UNIT) ignorées.
NOT_TESTED_GAME.
"""
import functools
import time
import uuid

from .journal import log
from .notify import game_notify

TITLE = 'S4Together'
RETRY_TICKS = 30
ACK_SECONDS = 5.0
# Contrôles successifs sans le commerce dans le jeu avant de le libérer : une lecture ratée ne libère rien.
RELEASE_AFTER = 3
KINDS = {'VET': 'vet', 'RETAIL': 'retail', 'RESTAURANT': 'restaurant', 'SMALL_BUSINESS': 'small_business'}
_UNKNOWN = object()


def kind_of(business_type):
    """Type serveur du type natif ; None pour les locations et types invalides."""
    name = getattr(business_type, 'name', None)
    if name in ('INVALID', 'RENTAL_UNIT') or not isinstance(name, str):
        return None
    return KINDS.get(name, 'other')


def businesses_of(snapshot):
    items = snapshot.get('businesses') if isinstance(snapshot, dict) else None
    return [item for item in items if isinstance(item, dict) and isinstance(item.get('lotId'), str)] \
        if isinstance(items, list) else []


def own_businesses(snapshot, household_id):
    """{lotId: kind} des commerces que le serveur attribue à notre foyer."""
    return {item['lotId']: item.get('kind') for item in businesses_of(snapshot)
            if item.get('householdId') == household_id}


def foreign_businesses(snapshot, household_id):
    """lotIds des commerces d'autres foyers : ni achat ni emménagement autorisé dans notre partie."""
    return {item['lotId'] for item in businesses_of(snapshot) if item.get('householdId') != household_id}


def game_owned_businesses(game_household_id):
    """{lotId: kind} des commerces possédés par ce foyer dans le jeu, _UNKNOWN si illisible."""
    try:
        import services
        from .home_lots import _desc
        trackers = services.business_service().get_business_trackers_for_household(int(game_household_id)) or ()
        persistence = services.get_persistence_service()
        owned = {}
        for tracker in trackers:
            kind = kind_of(tracker.business_type)
            if kind is None:
                continue
            for zone_id in tuple(tracker.business_managers.keys()):
                zone = persistence.get_zone_proto_buff(zone_id) if zone_id else None
                if zone is None or getattr(zone, 'active_plex', 0):
                    continue
                lot_id = _desc(zone.lot_description_id)
                if lot_id is not None:
                    owned[lot_id] = kind
        return owned
    except Exception as error:
        log('business_read', type(error).__name__)
        return _UNKNOWN


def game_zone_of(lot_id):
    """zone_id de ce terrain dans notre partie, ou None."""
    from .home_lots import game_local_lot
    local = game_local_lot(lot_id)
    zone = local.get('zoneId') if local else None
    return int(zone) if isinstance(zone, str) and zone.isdigit() else None


class BusinessGuard:
    """Refuse l'achat (hors chargement) d'un commerce réservé par un autre joueur."""

    def __init__(self, notify=game_notify):
        self.zones = frozenset()
        self.notify = notify

    def set_zones(self, zones):
        self.zones = frozenset(zones)

    def clear(self):
        self.zones = frozenset()

    def blocked(self, zone_id, from_load):
        return not from_load and bool(zone_id) and zone_id in self.zones

    def wrap(self, original):
        @functools.wraps(original)
        def wrapped(service, owner_household_id, business_type, zone_id, *args, **kwargs):
            from_load = kwargs.get('from_load', args[1] if len(args) > 1 else False)
            if self.blocked(zone_id, from_load):
                log('business_guard_block', 'foreign_business')
                try:
                    self.notify(TITLE, "Ce commerce appartient à un autre joueur du serveur : il ne peut pas être "
                                       "racheté.")
                except Exception:
                    pass
                return None
            return original(service, owner_household_id, business_type, zone_id, *args, **kwargs)
        wrapped.s4t_business_guard = self
        wrapped.s4t_original = original
        return wrapped


def install_game_guard():
    """BusinessGuard posé une seule fois sur BusinessService.make_owner ; renvoie la garde active."""
    from business.business_service import BusinessService
    guard = getattr(BusinessService.make_owner, 's4t_business_guard', None)
    if guard is None:
        guard = BusinessGuard()
        BusinessService.make_owner = guard.wrap(BusinessService.make_owner)
    return guard


def residence_guard():
    from sims.household import Household
    return getattr(Household.move_into_zone, 's4t_residence_guard', None)


class BusinessSync:
    """Appelée par AutoLink après HomeLotSync ; une seule requête en vol, jamais d'exception vers le jeu."""

    def __init__(self, read_owned=game_owned_businesses, zone_of=game_zone_of, guard=install_game_guard,
                 residence=residence_guard, notify=game_notify, new_id=lambda: str(uuid.uuid4()),
                 clock=time.monotonic):
        self.read_owned = read_owned
        self.zone_of = zone_of
        self.guard = guard
        self.residence = residence
        self.notify = notify
        self.new_id = new_id
        self.clock = clock
        self.informed = set()
        self.reset()

    def reset(self):
        self.waiting = None
        self.waiting_at = 0.0
        self.context = None
        self.cooldown = 0
        self.missing = {}
        self.refused = set()
        self.blocked_zones = set()
        # Garde native posée seulement au premier tick lié, jamais à la construction (menu principal).
        guard = getattr(self, 'active_guard', None)
        if guard is not None:
            guard.clear()
        residence = self._residence() if guard is not None else None
        if residence is not None and hasattr(residence, 'set_businesses'):
            residence.set_businesses(())

    def _guard(self):
        if getattr(self, 'active_guard', None) is None:
            try:
                self.active_guard = self.guard() if callable(self.guard) else self.guard
            except Exception as error:
                log('business_guard', type(error).__name__)
                return None
        return self.active_guard

    def _residence(self):
        try:
            return self.residence() if callable(self.residence) else self.residence
        except Exception:
            return None

    def tick(self, manager, character, snapshot):
        if character is None or character.get('status') != 'bound':
            return None
        household_id = character.get('householdId')
        context = (household_id, character.get('binding', {}).get('gameHouseholdId'))
        if self.context is not None and self.context != context:
            if self.waiting is not None and hasattr(manager, 'forget'):
                manager.forget(self.waiting[0])
            self.reset()
        self.context = context
        self._protect(snapshot, household_id)
        if self.waiting is not None:
            if self.clock() - self.waiting_at >= ACK_SECONDS:
                manager.request(self.waiting[1], dict(self.waiting[2]), self.waiting[0])
                self.waiting_at = self.clock()
            return None
        if self.cooldown > 0:
            self.cooldown -= 1
            return None
        self.cooldown = RETRY_TICKS
        owned = self.read_owned(character['binding']['gameHouseholdId'])
        if owned is _UNKNOWN:
            return None
        current = own_businesses(snapshot, household_id)
        foreign = foreign_businesses(snapshot, household_id)
        for lot_id in sorted(owned):
            if lot_id in foreign or lot_id in self.refused:
                self._inform('taken/' + lot_id, "Ce commerce est déjà réservé par un autre joueur du serveur.")
                continue
            if current.get(lot_id) != owned[lot_id]:
                return self._send(manager, 'business.claim', {'lotId': lot_id, 'householdId': household_id,
                                                              'kind': owned[lot_id]})
        for lot_id in sorted(current):
            if lot_id in owned:
                self.missing.pop(lot_id, None)
                continue
            self.missing[lot_id] = self.missing.get(lot_id, 0) + 1
            if self.missing[lot_id] >= RELEASE_AFTER:
                self.missing.pop(lot_id, None)
                return self._send(manager, 'business.release', {'lotId': lot_id})
        return None

    def flush(self, manager, character, snapshot):
        self.cooldown = 0
        return self.tick(manager, character, snapshot)

    def _protect(self, snapshot, household_id):
        zones = set()
        for lot_id in foreign_businesses(snapshot, household_id):
            zone = self.zone_of(lot_id)
            if zone:
                zones.add(zone)
        self.blocked_zones = zones
        guard = self._guard()
        if guard is not None:
            guard.set_zones(zones)
        residence = self._residence()
        if residence is not None and hasattr(residence, 'set_businesses'):
            residence.set_businesses(zones)

    def _send(self, manager, kind, payload):
        request_id = self.new_id()
        manager.request(kind, payload, request_id)
        self.waiting = (request_id, kind, dict(payload))
        self.waiting_at = self.clock()
        return kind

    def on_messages(self, manager, messages):
        for message in messages:
            if not isinstance(message, dict) or self.waiting is None or message.get('id') != self.waiting[0]:
                continue
            _, kind, payload = self.waiting
            self.waiting = None
            if message.get('ok') is True:
                log('business', kind + ':ok')
                # Nouveau snapshot attendu avant le prochain contrôle : pas de double envoi sur une vue périmée.
                self.cooldown = 1
                manager.request('snapshot', {}, self.new_id())
                continue
            code = message.get('code') if isinstance(message.get('code'), str) else 'error'
            log('business', kind + ':' + code)
            if kind == 'business.claim' and code == 'lot_taken':
                self.refused.add(payload['lotId'])
                self._inform('taken/' + payload['lotId'], "Ce commerce est déjà réservé par un autre joueur du "
                                                          "serveur.")
            elif code == 'unknown_type':
                # Serveur sans commerces (avant 0.1.35) : plus aucun envoi, la garde reste basée sur le snapshot.
                self.cooldown = 10 ** 9
                continue
            elif code == 'business_limit':
                self._inform('limit', "Limite de commerces réservés atteinte sur ce serveur.")
            self.cooldown = RETRY_TICKS

    def _inform(self, key, text):
        if key in self.informed:
            return
        self.informed.add(key)
        try:
            self.notify(TITLE, text)
        except Exception as error:
            log('business_notify', type(error).__name__)

    def status(self):
        return {'waiting': self.waiting[1] if self.waiting else None, 'refused': sorted(self.refused),
                'blockedZones': len(self.blocked_zones)}
