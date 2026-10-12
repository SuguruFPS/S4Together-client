"""Foyer « Nom (S4T) » de chaque autre joueur, créé dans MA partie multijoueur par les voies natives du jeu (12/10/2026).

Le Sim de l'ami est un vrai SimInfo du jeu : il se promène, se croise en ville, se parle comme un PNJ. Il porte
l'apparence native publiée par son joueur (appearance_sync) et habite la maison qu'il a réservée sur le serveur
(home_lots). Séquence reprise de interactions/utils/creation.pyc _CloneSimInfoSource._create_clone_sim_info et
du service de population (_move_household_into_zone → Household.move_into_zone), désassemblés dans 1.128 :
SimSpawner.create_sim_infos, SimInfo.save_sim(for_cloning=True), load_sim_info(is_clone=True), Household.name /
description / sim_info_gen / move_into_zone, SimInfo.is_instanced, resend_physical_attributes.

Garde-fous : uniquement dans la partie liée au serveur (CharacterSync « bound », jamais la partie solo) ; une seule
action par tick ; un foyer est reconnu par sa description `S4T:<playerId>:<sha16>` (pas de doublon après
rechargement) ; jamais d'apparence modifiée pendant que le Sim est instancié ; jamais d'emménagement dans un terrain
déjà habité dans cette partie : l'occupant natif (ZoneData.household_id) est relu juste avant move_into_zone.
Tant que l'apparence n'est pas appliquée et sauvegardée, la description porte `PENDING` au lieu de l'empreinte :
un clone à l'apparence aléatoire n'est ni renommé ni emménagé. NOT_TESTED_GAME.
"""
from .journal import log

PREFIX = 'S4T:'
LAST_NAME = 'S4T'
SUFFIX = ' (S4T)'
RETRY_TICKS = 60
MAX_ATTEMPTS = 3
MAX_GHOSTS = 16
# 16 caractères non hexadécimaux : ne peut égaler le début d'aucun SHA-256.
PENDING = 'pending-not-done'


def description(player_id, sha):
    return PREFIX + player_id + ':' + sha[:16]


def parse_description(text):
    """(playerId, sha16) d'un foyer S4T, sinon None. playerId peut contenir ':' : coupe sur le dernier."""
    if not isinstance(text, str) or not text.startswith(PREFIX):
        return None
    player, _, sha = text[len(PREFIX):].rpartition(':')
    return (player, sha) if player and len(sha) == 16 else None


def display_name(name):
    name = name.strip()[:40] if isinstance(name, str) and name.strip() else 'Joueur'
    return name


def plan(players, ghosts, homes):
    """Prochaine action, ou None. Python pur.

    players : {playerId: {'name', 'sha256', 'data'}} — apparences validées des autres joueurs.
    ghosts : {playerId: {'householdId', 'sha16', 'firstName', 'householdName', 'zoneId', 'instanced'}} — foyers S4T.
    homes : {playerId: {'zoneId', 'occupiedLocally'}} — maison résolue dans cette partie (home_lots).
    """
    for player_id in sorted(players):
        player = players[player_id]
        name = display_name(player.get('name'))
        ghost = ghosts.get(player_id)
        if ghost is None:
            if len(ghosts) >= MAX_GHOSTS:
                continue
            return ('create', player_id, {'name': name, 'sha256': player['sha256'], 'data': player['data']})
        if ghost['sha16'] != player['sha256'][:16] and not ghost['instanced']:
            return ('appearance', player_id, {'householdId': ghost['householdId'], 'sha256': player['sha256'],
                                              'data': player['data']})
        if ghost['sha16'] == PENDING:
            # Apparence jamais appliquée : ni renommage ni emménagement de ce clone.
            continue
        if ghost['firstName'] != name or ghost['householdName'] != name + SUFFIX:
            return ('rename', player_id, {'householdId': ghost['householdId'], 'name': name})
        home = homes.get(player_id)
        zone_id = home.get('zoneId') if home else None
        if zone_id and str(ghost.get('zoneId') or '') != zone_id and home.get('occupiedLocally') is False:
            return ('move', player_id, {'householdId': ghost['householdId'], 'zoneId': zone_id})
    return None


def check_move(household_id, active_household_id, zone):
    """Lève si `household_id` ne doit pas emménager dans `zone` (ZoneData relu à l'instant). Python pur."""
    if not household_id or household_id == active_household_id:
        raise PermissionError('local_household')
    if zone is None or getattr(zone, 'active_plex', 0):
        raise LookupError('zone')
    occupant = zone.household_id
    if occupant and occupant != household_id:
        raise PermissionError('occupied')


class RemoteHouseholds:
    """Appelée par AutoLink après home_lots et appearance_sync. Jamais d'exception vers le jeu."""

    def __init__(self, adapter=None):
        self.adapter = adapter if adapter is not None else GameHouseholds()
        self.attempts = {}
        self.cooldown = 0
        self.last = None
        self.ghosts = {}

    def reset(self):
        # Nouvelle zone : foyers relus ; les échecs déjà comptés restent (pas de boucle de création ratée).
        self.cooldown = 0
        self.ghosts = {}

    def tick(self, character, names, appearances, homes):
        if character is None or character.get('status') != 'bound':
            return None
        if self.cooldown > 0:
            self.cooldown -= 1
            return None
        self.ghosts = self.adapter.scan()
        players = {pid: {'name': names.get(pid), 'sha256': a['sha256'], 'data': a['data']}
                   for pid, a in appearances.items()}
        homes_by_player = {h['playerId']: h for h in homes if isinstance(h, dict) and 'playerId' in h}
        players = {pid: p for pid, p in players.items() if self.attempts.get(pid, 0) < MAX_ATTEMPTS}
        action = plan(players, self.ghosts, homes_by_player)
        if action is None:
            return None
        kind, player_id, args = action
        try:
            getattr(self.adapter, kind)(player_id, **args)
            self.attempts.pop(player_id, None)
            log('remote_household', kind + ':ok')
            self.last = kind
        except Exception as error:
            self.attempts[player_id] = self.attempts.get(player_id, 0) + 1
            self.cooldown = RETRY_TICKS
            log('remote_household', kind + ':' + type(error).__name__)
            self.last = kind + ':' + type(error).__name__
        return kind

    def status(self):
        return {'ghosts': {pid: {'zoneId': g.get('zoneId'), 'instanced': g.get('instanced')} for pid, g in self.ghosts.items()},
                'failed': sorted(pid for pid, n in self.attempts.items() if n >= MAX_ATTEMPTS),
                'last': self.last, 'gameReadiness': 'NOT_TESTED_GAME'}


class GameHouseholds:
    """Adaptateur du jeu. Chaque méthode lève en cas d'échec ; RemoteHouseholds compte et espace les essais."""

    def _household(self, household_id):
        import services
        household = services.household_manager().get(int(household_id))
        if household is None:
            raise LookupError('household')
        return household

    def _sim_info(self, household):
        sim_infos = list(household.sim_info_gen())
        if len(sim_infos) != 1:
            raise LookupError('sim_info')
        return sim_infos[0]

    def _own(self, household, player_id):
        """Empreinte du foyer S4T de ce joueur exactement ; lève pour tout autre foyer, dont celui du joueur local."""
        import services
        parsed = parse_description(getattr(household, 'description', None))
        if parsed is None or parsed[0] != player_id or household.id == services.active_household_id():
            raise PermissionError('not_remote_household')
        return parsed[1]

    def scan(self):
        import services
        found = {}
        for household in tuple(services.household_manager().values()):
            parsed = parse_description(getattr(household, 'description', None))
            if parsed is None or parsed[0] in found:
                continue
            try:
                sim_info = self._sim_info(household)
            except LookupError:
                continue
            found[parsed[0]] = {'householdId': str(household.id), 'sha16': parsed[1], 'firstName': sim_info.first_name,
                                'householdName': household.name, 'zoneId': str(household.home_zone_id or ''),
                                'instanced': bool(sim_info.is_instanced(allow_hidden_flags=0xFFFFFFFF))}
        return found

    def _appearance(self, data):
        from protocolbuffers import FileSerialization_pb2
        appearance = FileSerialization_pb2.SimData()
        appearance.ParseFromString(data)
        return appearance

    def _apply(self, sim_info, appearance):
        """Fusion clone : champs d'apparence remplacés dans la sauvegarde complète du Sim, identité restaurée."""
        from sims.sim_info_lod import SimInfoLODLevel
        proto = sim_info.save_sim(for_cloning=True)
        for field, _ in appearance.ListFields():
            proto.ClearField(field.name)
        proto.MergeFrom(appearance)
        base = sim_info._base
        kept = {name: getattr(base, name) for name in ('first_name', 'last_name', 'first_name_key', 'last_name_key',
                                                        'full_name_key', 'breed_name', 'breed_name_key')}
        sim_id, household_id = sim_info.sim_id, sim_info._household_id
        sim_info.load_sim_info(proto, is_clone=True, default_lod=SimInfoLODLevel.FULL)
        sim_info.sim_id = sim_id
        sim_info._household_id = household_id
        for name, value in kept.items():
            setattr(base, name, value)
        sim_info.resend_physical_attributes()

    def create(self, player_id, name, sha256, data):
        from sims.sim_info_lod import SimInfoLODLevel
        from sims.sim_info_types import Age, Gender
        from sims.sim_spawner import SimCreator, SimSpawner
        appearance = self._appearance(data)
        creator = SimCreator(gender=Gender(appearance.gender), age=Age(appearance.age), first_name=name, last_name=LAST_NAME)
        # household=None : le jeu crée un foyer neuf, l'ajoute au gestionnaire et y range le Sim (sim_spawner 1.128).
        sim_infos, household = SimSpawner.create_sim_infos((creator,), household=None, generate_deterministic_sim=True,
                                                           creation_source='cloning')
        sim_info = sim_infos[0]
        # En attente d'abord : si la suite échoue, le foyer est retrouvé sans être réputé à jour.
        household.description = description(player_id, PENDING)
        self._apply(sim_info, appearance)
        sim_info.set_default_data()
        sim_info.save_sim()
        household.name = name + SUFFIX
        household.description = description(player_id, sha256)
        household.save_data()
        # Foyer non actif : même fin que le clonage natif.
        sim_info.request_lod(SimInfoLODLevel.BASE)
        sim_info.resend_physical_attributes()
        sim_info.relationship_tracker.clean_and_send_remaining_relationship_info()
        log('remote_household', 'created:' + str(household.id))

    def appearance(self, player_id, householdId, sha256, data):
        household = self._household(householdId)
        sim_info = self._sim_info(household)
        if sim_info.is_instanced(allow_hidden_flags=0xFFFFFFFF):
            raise RuntimeError('instanced')
        self._own(household, player_id)
        appearance = self._appearance(data)
        household.description = description(player_id, PENDING)
        self._apply(sim_info, appearance)
        sim_info.save_sim()
        household.description = description(player_id, sha256)

    def rename(self, player_id, householdId, name):
        household = self._household(householdId)
        sim_info = self._sim_info(household)
        if self._own(household, player_id) == PENDING:
            raise RuntimeError('pending')
        sim_info.first_name = name
        sim_info.last_name = LAST_NAME
        household.name = name + SUFFIX
        sim_info.save_sim()

    def move(self, player_id, householdId, zoneId):
        import services
        household = self._household(householdId)
        if self._own(household, player_id) == PENDING:
            raise RuntimeError('pending')
        # Relu juste avant d'emménager : seul notre propre foyer distant peut déjà occuper le terrain.
        zone = services.get_persistence_service().get_zone_proto_buff(int(zoneId))
        check_move(household.id, services.active_household_id(), zone)
        household.move_into_zone(int(zoneId))
