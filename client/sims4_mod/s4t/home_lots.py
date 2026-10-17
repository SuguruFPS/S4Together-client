"""Maison de chaque joueur : publiée au serveur et retrouvée dans la partie des autres (12/10/2026).

Chaque joueur a sa propre sauvegarde : `zone_id` et `lot_id` y sont tirés au hasard et ne désignent pas le même
terrain d'une partie à l'autre. L'identifiant commun est `lot_description_id`, la description de terrain livrée
avec le monde (même valeur dans toutes les sauvegardes du même monde). C'est lui qui sert de `lotId` serveur.

API vérifiée par désassemblage de simulation.zip / generated.zip 1.128 :
services.household_manager().get(id), Household.home_zone_id ;
services.get_persistence_service().get_zone_proto_buff(zone_id) et .zone_proto_buffs_gen() ;
EA.Sims4.Persistence.ZoneData : zone_id, world_id, lot_description_id, name, household_id, active_plex.
Appartements (active_plex) refusés : plusieurs logements y partagent la même description.

Ce module ne crée, ne déplace ni ne supprime aucun foyer : il publie et il résout. Le foyer distant visible
dépend du transport d'apparence (docs/REMOTE_RENDER_SPIKE_2026-10-12.md). NOT_TESTED_GAME.
"""
import re
import uuid
import time

from .journal import log
from .notify import game_notify

TITLE = 'S4Together'
SUFFIX = ' (S4T)'
RETRY_TICKS = 30
ACK_SECONDS = 5.0
_NATIVE = re.compile(r'^[0-9]{1,20}$')
_UNKNOWN = object()


def _desc(value):
    return str(value) if type(value) is int and 0 < value < 2 ** 64 else None


def game_home_lot(household_id):
    """Description du terrain habité par ce foyer, None s'il est sans domicile, _UNKNOWN si illisible."""
    try:
        import services
        household = services.household_manager().get(int(household_id))
        if household is None:
            return _UNKNOWN
        zone_id = household.home_zone_id
        if not zone_id:
            return None
        zone = services.get_persistence_service().get_zone_proto_buff(zone_id)
        if zone is None or getattr(zone, 'active_plex', 0):
            return _UNKNOWN
        return _desc(zone.lot_description_id) or _UNKNOWN
    except Exception as error:
        log('home_lot_read', type(error).__name__)
        return _UNKNOWN


def game_local_lot(lot_description_id):
    """Terrain de cette partie portant la même description : {zoneId, name, occupied} ou None."""
    try:
        import services
        wanted = int(lot_description_id)
        for zone in services.get_persistence_service().zone_proto_buffs_gen():
            if zone.lot_description_id == wanted and not getattr(zone, 'active_plex', 0):
                from .residence import replaceable
                occupant = services.household_manager().get(zone.household_id) if zone.household_id else None
                return {'zoneId': str(zone.zone_id), 'name': str(zone.name or ''), 'occupied': bool(zone.household_id),
                        'replaceableNpc': replaceable(occupant, services.active_household_id(), zone.zone_id)}
    except Exception as error:
        log('home_lot_resolve', type(error).__name__)
    return None


def lots_of(snapshot):
    lots = snapshot.get('lots') if isinstance(snapshot, dict) else None
    return [lot for lot in lots if isinstance(lot, dict)] if isinstance(lots, list) else []


def own_lot(snapshot, household_id):
    """lotId serveur actuellement réservé par notre foyer serveur, ou None."""
    for lot in lots_of(snapshot):
        if lot.get('householdId') == household_id and isinstance(lot.get('id'), str):
            return lot['id']
    return None


def taken_by_other(snapshot, lot_id, household_id):
    """Vrai si le snapshot serveur donne déjà ce terrain à un autre foyer."""
    for lot in lots_of(snapshot):
        if lot.get('id') == lot_id and isinstance(lot.get('ownerId'), str) and lot.get('householdId') != household_id:
            return True
    return False


def player_names(snapshot):
    """{playerId: nom} : `residents` nomme aussi les propriétaires hors ligne ; `players` (connectés) prime."""
    names = {}
    for key in ('residents', 'players'):
        people = snapshot.get(key) if isinstance(snapshot, dict) else None
        for person in people if isinstance(people, list) else []:
            if isinstance(person, dict) and isinstance(person.get('playerId'), str):
                names[person['playerId']] = person.get('name')
    return names


def remote_homes(snapshot, household_id):
    """Maisons natives des autres joueurs : [{playerId, name, lotId}] triées, sans la nôtre."""
    names = player_names(snapshot)
    homes = []
    for lot in lots_of(snapshot):
        owner, lot_id = lot.get('ownerId'), lot.get('id')
        if not isinstance(owner, str) or not isinstance(lot_id, str) or not _NATIVE.match(lot_id):
            continue
        if lot.get('householdId') == household_id:
            continue
        name = names.get(owner)
        name = name.strip()[:40] if isinstance(name, str) and name.strip() else 'Joueur'
        homes.append({'playerId': owner, 'name': name + SUFFIX, 'lotId': lot_id})
    return sorted(homes, key=lambda home: home['lotId'])


class HomeLotSync:
    """Appelée par AutoLink après CharacterSync ; une seule requête en vol, jamais d'exception vers le jeu."""

    def __init__(self, read_home=game_home_lot, resolve=game_local_lot, notify=game_notify,
                 new_id=lambda: str(uuid.uuid4()), clock=time.monotonic):
        self.read_home = read_home
        self.resolve = resolve
        self.notify = notify
        self.new_id = new_id
        self.clock = clock
        self.informed = set()
        self.reset()

    def reset(self):
        self.waiting = None
        self.waiting_body = None
        self.waiting_at = 0.0
        self.context = None
        self.confirmed = None
        self.refresh = None
        self.cooldown = 0
        self.refused = None
        # Vrai quand le refus vient du snapshot : il tombe dès que le snapshot montre la maison libre.
        self.seen_taken = False
        self.homes = []
        # Un serveur sans déménagement atomique ne doit jamais faire perdre l'ancien domicile.
        self.no_move = False

    def tick(self, manager, character, snapshot):
        if character is None or character.get('status') != 'bound':
            return None
        context = (character.get('householdId'), character.get('binding', {}).get('gameHouseholdId'))
        if self.context is not None and self.context != context:
            for pending in (self.waiting, self.refresh):
                if pending is not None and hasattr(manager, 'forget'):
                    manager.forget(pending[0])
            self.reset()
        self.context = context
        self._resolve_remote(snapshot, character.get('householdId'))
        if self.waiting is not None:
            if self.clock() - self.waiting_at >= ACK_SECONDS:
                manager.request(self.waiting[1], dict(self.waiting_body), self.waiting[0])
                self.waiting_at = self.clock()
            return None
        if self.confirmed is not None:
            if own_lot(snapshot, character.get('householdId')) != self.confirmed[0]:
                self._refresh(manager)
                return None
            if self.refresh is not None and hasattr(manager, 'forget'):
                manager.forget(self.refresh[0])
            self.confirmed, self.refresh = None, None
        if self.cooldown > 0:
            self.cooldown -= 1
            return None
        desired = self.read_home(character['binding']['gameHouseholdId'])
        if desired is _UNKNOWN:
            return None
        current = own_lot(snapshot, character.get('householdId'))
        if desired == current:
            self.refused = None
            return None
        if desired is not None and taken_by_other(snapshot, desired, character.get('householdId')):
            # Déménagement vers une maison déjà habitée sur le serveur : l'ancien domicile n'est pas libéré
            # pour un claim qui serait refusé (lot_taken).
            self.seen_taken = True
            if desired != self.refused:
                self.refused = desired
                log('home_lot', 'move_blocked:lot_taken')
                self._inform('taken/' + desired, "Cette maison est déjà habitée par un autre joueur du serveur : "
                                                 "S4Together ne l'enregistre pas pour toi.")
            return None
        if desired is not None and desired == self.refused:
            if not self.seen_taken:
                # Refus du serveur que le snapshot ne montre pas encore : ni nouvel essai ni libération de l'ancien.
                return None
            self.refused, self.seen_taken = None, False
        if current is not None and desired is not None:
            if self.no_move:
                return None
            # Déménagement atomique côté serveur : l'ancien terrain n'est libéré que si le nouveau est accordé.
            return self._send(manager, 'property.move', {'fromLotId': current, 'lotId': desired,
                                                         'householdId': character['householdId']})
        if current is not None:
            # Le foyer est devenu sans domicile dans le jeu ; ce n'est pas un déménagement.
            return self._send(manager, 'property.release', {'lotId': current})
        return self._send(manager, 'property.claim', {'lotId': desired, 'householdId': character['householdId']})

    def flush(self, manager, character, snapshot):
        """Sauvegarde du jeu ou déchargement de zone : domicile republié sans attendre le délai de nouvel essai."""
        self.cooldown = 0
        return self.tick(manager, character, snapshot)

    def _send(self, manager, kind, payload):
        request_id = self.new_id()
        manager.request(kind, payload, request_id)
        self.waiting = (request_id, kind, payload.get('lotId'))
        self.waiting_body = dict(payload)
        self.waiting_at = self.clock()
        return kind

    def _refresh(self, manager):
        if self.refresh is None or self.clock() - self.refresh[1] >= ACK_SECONDS:
            request_id = self.refresh[0] if self.refresh else self.new_id()
            manager.request('snapshot', {}, request_id)
            self.refresh = (request_id, self.clock())

    def on_messages(self, manager, messages):
        for message in messages:
            if not isinstance(message, dict) or self.waiting is None or message.get('id') != self.waiting[0]:
                continue
            _, kind, lot_id = self.waiting
            self.waiting = None
            self.waiting_body = None
            if message.get('ok') is True:
                log('home_lot', kind + ':ok')
                # Attend la propriété canonique, jamais un second déménagement basé sur une ancienne vue.
                self.confirmed = (None if kind == 'property.release' else lot_id,)
                self._refresh(manager)
                continue
            code = message.get('code') if isinstance(message.get('code'), str) else 'error'
            log('home_lot', kind + ':' + code)
            if kind == 'property.move' and code in ('unknown_type', 'invalid_request'):
                self.no_move = True
                self._inform('move_unsupported', "Le serveur n'a pas accepté le déménagement sécurisé. "
                             "Ton ancienne maison reste réservée. Une mise à jour du serveur peut être nécessaire.")
                continue
            self.cooldown = RETRY_TICKS
            if kind in ('property.claim', 'property.move') and code == 'lot_taken':
                # Un autre joueur habite déjà cette maison sur le serveur : pas de nouvel essai en boucle.
                self.refused, self.seen_taken = lot_id, False
                self._inform('taken/' + lot_id, "Cette maison est déjà habitée par un autre joueur du serveur : "
                                                "S4Together ne l'enregistre pas pour toi.")

    def _resolve_remote(self, snapshot, household_id):
        homes = []
        for home in remote_homes(snapshot, household_id):
            # Relu à chaque tick, jamais mis en cache : `occupied` périmé ferait emménager dans un terrain habité.
            local = self.resolve(home['lotId'])
            entry = dict(home, zoneId=local['zoneId'] if local else None, lotName=local['name'] if local else None,
                         occupiedLocally=local['occupied'] if local else None)
            if local and local.get('replaceableNpc') is True:
                entry['replaceableNpc'] = True
            homes.append(entry)
            if local is not None:
                place = '« {} »'.format(local['name']) if local['name'] else 'un terrain de ton monde'
                self._inform('home/' + home['playerId'] + '/' + home['lotId'],
                             '{} habite {}.'.format(home['name'], place))
        self.homes = homes

    def _inform(self, key, text):
        if key in self.informed:
            return
        self.informed.add(key)
        self.notify(TITLE, text)

    def status(self):
        """Diagnostic : maisons des autres joueurs et leur terrain dans cette partie."""
        return {'waiting': self.waiting[1] if self.waiting else None, 'refusedLot': self.refused,
                'remoteHomes': [dict(home) for home in self.homes]}
