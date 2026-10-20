"""Calendrier commun (#166, 20/10/2026) : les fêtes créées par un joueur (pack Saisons) apparaissent dans le calendrier
des autres joueurs ; seul leur créateur les modifie ou les supprime. Le serveur garde chaque fête (`calendar.set`,
`calendar.remove`, révision CAS) ; chaque client recopie les fêtes des autres et publie les siennes.

Liens : identifiant serveur -> identifiant local de la fête, gardés par personnage dans ConfigStore. Seule une fête liée
puis disparue du calendrier local compte comme supprimée par son créateur ; une fête non liée n'est jamais supprimée.

API vérifiée par désassemblage de simulation.zip et generated.zip (Python 3.7) :
services.holiday_service() (None sans Saisons) ; _holidays {id: CustomHoliday} ; _holiday_times {SeasonLength: YearOfHolidays},
clé courante services.season_service().season_length_option ; YearOfHolidays._holidays[saison][jour] = id ;
add_a_holiday(proto, season, day) crée l'id par generate_object_id() et ne le renvoie pas ; remove_a_holiday(id) ;
CustomHoliday._holiday_type None pour une fête de joueur, _name (texte), _icon (Key type/group/instance), _traditions
(.guid64), _time_off_work, _time_off_school, _lot_decoration_preset (.guid64) ;
protocolbuffers.GameplaySaveData_pb2.Holiday : holiday_type, name, icon (ResourceKey), time_off_for_work,
time_off_for_school, traditions (fixed64), lot_decoration_preset (fixed64).
"""
import time
import uuid

from .journal import log

POLL_SECONDS = 5.0
ACK_SECONDS = 15.0
MAX_SEASON = 3
MAX_DAY = 28
MAX_NAME = 64
MAX_TRADITIONS = 16
HOLIDAY_KEYS = ('name', 'traditions', 'icon', 'timeOffWork', 'timeOffSchool', 'decorationPreset')
# Refus durables : la fête locale n'est plus proposée au serveur pendant cette session.
DURABLE_REFUSALS = ('forbidden', 'not_found', 'calendar_limit', 'invalid_request')
_DIGITS = frozenset('0123456789')
_ID_CHARS = frozenset('0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ-')


def _whole(value):
    return isinstance(value, int) and not isinstance(value, bool)


def game_id(value):
    return isinstance(value, str) and 1 <= len(value) <= 20 and set(value) <= _DIGITS and int(value) < 2 ** 64


def valid_holiday(holiday):
    if not isinstance(holiday, dict) or set(holiday) != set(HOLIDAY_KEYS):
        return False
    name = holiday['name']
    if not isinstance(name, str) or not name.strip() or len(name) > MAX_NAME or any(ord(c) < 32 or 127 <= ord(c) < 160 for c in name):
        return False
    traditions, icon = holiday['traditions'], holiday['icon']
    if not isinstance(traditions, list) or len(traditions) > MAX_TRADITIONS or not all(game_id(t) for t in traditions):
        return False
    if icon is not None and (not isinstance(icon, list) or len(icon) != 3 or not all(game_id(i) for i in icon)):
        return False
    return (isinstance(holiday['timeOffWork'], bool) and isinstance(holiday['timeOffSchool'], bool)
            and game_id(holiday['decorationPreset']))


def valid_day(season, day):
    return _whole(season) and _whole(day) and 0 <= season <= MAX_SEASON and 0 <= day <= MAX_DAY


def valid_entry(entry):
    if not isinstance(entry, dict):
        return False
    entry_id = entry.get('entryId')
    if not isinstance(entry_id, str) or not 8 <= len(entry_id) <= 64 or not set(entry_id) <= _ID_CHARS:
        return False
    if not isinstance(entry.get('ownerId'), str) or not entry['ownerId']:
        return False
    revision = entry.get('revision')
    return (_whole(revision) and revision >= 1 and valid_day(entry.get('season'), entry.get('day'))
            and valid_holiday(entry.get('holiday')))


def content(item):
    """Contenu comparable d'une entrée serveur ou d'une fête locale ({season, day, holiday})."""
    h = item['holiday']
    return (item['season'], item['day'], h['name'], tuple(h['traditions']), tuple(h['icon'] or ()),
            h['timeOffWork'], h['timeOffSchool'], h['decorationPreset'])


def signature(item):
    """Même fête vue des deux côtés quand le lien est perdu : saison, jour et nom."""
    return (item['season'], item['day'], item['holiday']['name'])


class _MemoryLinks:
    def __init__(self):
        self.values = {}

    def calendar_links(self):
        return dict(self.values)

    def link_calendar(self, key, value):
        if value is None:
            self.values.pop(key, None)
        else:
            self.values[key] = value


class CalendarSync:
    def __init__(self, store=None, game=None, clock=time.monotonic, new_id=None):
        self.store = store if store is not None else _MemoryLinks()
        self.game = game if game is not None else GameCalendar()
        self.clock = clock
        self.new_id = new_id or (lambda: str(uuid.uuid4()))
        self.entries = {}
        self.ready = False
        self.disabled = False
        self.ignored = set()
        self.failed = {}
        self.waiting = None
        self.waiting_at = 0.0
        self.next_check = 0.0
        self.character_id = None

    # --- messages serveur ---
    def on_messages(self, manager, messages):
        for message in messages:
            if not isinstance(message, dict):
                continue
            payload = message.get('payload') if isinstance(message.get('payload'), dict) else {}
            if message.get('type') == 'snapshot':
                self._adopt(payload)
            elif message.get('type') == 'calendar':
                self._event(payload)
            elif self.waiting is not None and message.get('id') == self.waiting[0]:
                self._reply(manager, message, payload)

    def _adopt(self, snapshot):
        views = snapshot.get('calendar') if isinstance(snapshot, dict) else None
        if not isinstance(views, list):
            return
        self.entries = {e['entryId']: e for e in views if valid_entry(e)}
        self.ready = True

    def _event(self, payload):
        entry = payload.get('entry')
        if valid_entry(entry):
            known = self.entries.get(entry['entryId'])
            if known is None or known['revision'] <= entry['revision']:
                self.entries[entry['entryId']] = entry
        elif isinstance(payload.get('removed'), str):
            self.entries.pop(payload['removed'], None)

    def _reply(self, manager, message, payload):
        _, kind, _, entry_id, local_id = self.waiting
        self.waiting = None
        if message.get('ok') is True:
            if kind == 'calendar.set':
                self._event(payload)
                self._link(entry_id, local_id)
            else:
                self.entries.pop(entry_id, None)
                self._link(entry_id, None)
            return
        code = message.get('code') if isinstance(message.get('code'), str) else 'error'
        log('calendar_sync', kind + ':' + code)
        if code == 'unknown_type':
            # Serveur sans calendrier commun (avant 0.1.48) : aucune nouvelle tentative.
            self.disabled = True
        elif code == 'stale_revision':
            manager.request('snapshot', {}, self.new_id())
        elif code == 'not_found' and kind == 'calendar.remove':
            self._link(entry_id, None)
        elif code in DURABLE_REFUSALS and local_id is not None:
            self.ignored.add(local_id)

    # --- liens persistants (par personnage) ---
    def _key(self, entry_id):
        return self.character_id + '/' + entry_id

    def _links(self):
        prefix = self.character_id + '/'
        return {k[len(prefix):]: v for k, v in self.store.calendar_links().items() if k.startswith(prefix)}

    def _link(self, entry_id, local_id):
        if self.character_id is not None:
            self.store.link_calendar(self._key(entry_id), local_id)

    # --- boucle ---
    def tick(self, manager, character):
        if self.disabled:
            return None
        character_id = character.get('characterId') if isinstance(character, dict) else None
        if not isinstance(character_id, str) or not character_id:
            return None
        if character_id != self.character_id:
            self.character_id = character_id
            self.waiting, self.ignored, self.failed = None, set(), {}
        now = self.clock()
        if self.waiting is not None:
            if now - self.waiting_at >= ACK_SECONDS:
                manager.request(self.waiting[1], dict(self.waiting[2]), self.waiting[0])
                self.waiting_at = now
            return None
        if now < self.next_check:
            return None
        self.next_check = now + POLL_SECONDS
        if not self.ready:
            self._adopt(getattr(manager, 'snapshot', None))
        identity = getattr(manager, 'identity', None)
        player_id = identity.get('playerId') if isinstance(identity, dict) else None
        if not self.ready or not isinstance(player_id, str) or not player_id:
            return None
        local = self.game.local()
        if local is None:
            return None
        return self._reconcile(manager, player_id, local, now)

    def _reconcile(self, manager, player_id, local, now):
        claimed = set()
        for entry_id, local_id in sorted(self._links().items()):
            entry = self.entries.get(entry_id)
            if entry is None:
                # Supprimée par son créateur : retirée ici aussi.
                if local_id in local:
                    self.game.remove(local_id)
                    claimed.add(local_id)
                    log('calendar_sync', 'removed')
                self._link(entry_id, None)
            elif local_id not in local:
                if entry['ownerId'] == player_id:
                    return self._send(manager, 'calendar.remove', {'entryId': entry_id, 'expectedRevision': entry['revision']},
                                      entry_id, None, now)
                # Fête d'un autre joueur effacée ici : elle reste commune, on la remet.
                self._link(entry_id, None)
            else:
                claimed.add(local_id)
                if content(local[local_id]) == content(entry):
                    continue
                if entry['ownerId'] == player_id:
                    return self._publish(manager, entry_id, entry['revision'], local_id, local[local_id], now)
                self._place(entry, local_id)
        links = self._links()
        for entry_id, entry in sorted(self.entries.items()):
            if entry_id in links:
                continue
            match = next((lid for lid, item in sorted(local.items())
                          if lid not in claimed and signature(item) == signature(entry)), None)
            if match is not None:
                claimed.add(match)
                self._link(entry_id, match)
            elif self.failed.get(entry_id) != entry['revision']:
                self._place(entry, None)
        for local_id, item in sorted(local.items()):
            if local_id in claimed or local_id in self.ignored:
                continue
            if not valid_day(item.get('season'), item.get('day')) or not valid_holiday(item.get('holiday')):
                self.ignored.add(local_id)
                continue
            return self._publish(manager, self.new_id(), 0, local_id, item, now)
        return None

    def _place(self, entry, local_id):
        """Pose (ou remplace) la fête d'un autre joueur dans le calendrier local."""
        if local_id is not None:
            self.game.remove(local_id)
        new_id = self.game.add(entry)
        if new_id is None:
            # Jour déjà occupé par une autre fête : pas de nouvel essai avant une nouvelle révision.
            self.failed[entry['entryId']] = entry['revision']
            self._link(entry['entryId'], None)
            log('calendar_sync', 'place_failed')
            return
        self.failed.pop(entry['entryId'], None)
        self._link(entry['entryId'], new_id)
        log('calendar_sync', 'placed')

    def _publish(self, manager, entry_id, revision, local_id, item, now):
        payload = {'entryId': entry_id, 'expectedRevision': revision, 'season': item['season'], 'day': item['day'],
                   'holiday': dict(item['holiday'])}
        return self._send(manager, 'calendar.set', payload, entry_id, local_id, now)

    def _send(self, manager, kind, payload, entry_id, local_id, now):
        request_id = self.new_id()
        manager.request(kind, payload, request_id)
        self.waiting = (request_id, kind, payload, entry_id, local_id)
        self.waiting_at = now
        return kind


def _pairs(container):
    """(clé, valeur) d'un dictionnaire ou d'une liste indexée (rangement interne du jeu)."""
    return container.items() if hasattr(container, 'items') else enumerate(container)


class GameCalendar:
    """Accès au calendrier du jeu ; local() renvoie None sans le pack Saisons."""

    @staticmethod
    def _service():
        import services
        getter = getattr(services, 'holiday_service', None)
        return getter() if getter is not None else None

    def local(self):
        import services
        service = self._service()
        if service is None:
            return None
        year = service._holiday_times.get(services.season_service().season_length_option)
        if year is None:
            return None
        found = {}
        for season, days in _pairs(year._holidays):
            for day, holiday_id in _pairs(days):
                holiday = service._holidays.get(holiday_id)
                if holiday is None or holiday._holiday_type is not None or not isinstance(holiday._name, str):
                    continue
                found[str(holiday_id)] = {'season': int(season), 'day': int(day), 'holiday': self._read(holiday)}
        return found

    @staticmethod
    def _read(holiday):
        icon = holiday._icon
        preset = holiday._lot_decoration_preset
        return {'name': holiday._name,
                'traditions': [str(t.guid64) for t in holiday._traditions][:MAX_TRADITIONS],
                'icon': None if icon is None else [str(icon.type), str(icon.group), str(icon.instance)],
                'timeOffWork': bool(holiday._time_off_work), 'timeOffSchool': bool(holiday._time_off_school),
                'decorationPreset': str(preset.guid64) if preset is not None else '0'}

    def add(self, entry):
        from protocolbuffers.GameplaySaveData_pb2 import Holiday
        from seasons.seasons_enums import SeasonType
        service = self._service()
        if service is None:
            return None
        h = entry['holiday']
        proto = Holiday()
        proto.holiday_type = 0
        proto.name = h['name']
        proto.traditions.extend(int(t) for t in h['traditions'])
        if h['icon'] is not None:
            proto.icon.type, proto.icon.group, proto.icon.instance = (int(v) for v in h['icon'])
        proto.time_off_for_work = h['timeOffWork']
        proto.time_off_for_school = h['timeOffSchool']
        proto.lot_decoration_preset = int(h['decorationPreset'])
        before = set(service._holidays)
        service.add_a_holiday(proto, SeasonType(entry['season']), entry['day'])
        created = [i for i in service._holidays if i not in before]
        if len(created) != 1:
            return None
        # Jour occupé : la fête existe mais n'est placée dans aucune année ; on la retire.
        placed = self.local()
        if placed is not None and str(created[0]) not in placed:
            service.remove_a_holiday(created[0])
            return None
        return str(created[0])

    def remove(self, local_id):
        service = self._service()
        if service is not None and int(local_id) in service._holidays:
            service.remove_a_holiday(int(local_id))
