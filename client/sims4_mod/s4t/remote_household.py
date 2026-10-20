"""Foyer « Nom (S4T) » de chaque autre joueur, créé dans MA partie multijoueur par les voies natives du jeu (12/10/2026).

Le Sim de l'ami est un vrai SimInfo du jeu, représenté par une copie passive protégée par offline_guard. Il porte
l'apparence native publiée par son joueur (appearance_sync) et habite la maison qu'il a réservée sur le serveur
(home_lots). Séquence reprise de interactions/utils/creation.pyc _CloneSimInfoSource._create_clone_sim_info et
du service de population (_move_household_into_zone → Household.move_into_zone), désassemblés dans 1.128 :
SimSpawner.create_sim_infos, SimInfo.save_sim(for_cloning=True), load_sim_info(is_clone=True), Household.name /
description / sim_info_gen / move_into_zone, SimInfo.is_instanced, resend_physical_attributes.

Garde-fous : uniquement dans la partie liée au serveur (CharacterSync « bound », jamais la partie solo) ; une seule
action par tick ; un foyer est reconnu par sa description `S4T:<playerId>:<sha16>` (pas de doublon après
rechargement) ; jamais d'apparence modifiée pendant que le Sim est instancié. Occupant natif relu avant move_into_zone :
seul un PNJ non joué et non instancié peut libérer une résidence canonique, après journal durable de son domicile.
Tout foyer joué, sélectionnable, S4T ou incertain reste protégé. Aucun Sim supprimé ; restauration du PNJ sur lot libéré.
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
# Appels de _clear_npc_home avant de redemander le départ d'un PNJ resté sur le terrain chargé.
LEAVE_RETRY_CALLS = 12
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


def plan(players, ghosts, homes, names=None, outfits=None):
    """Prochaine action, ou None. Python pur.

    players : {playerId: {'name', 'sha256', 'data'}} — apparences validées des autres joueurs.
    ghosts : {playerId: {'householdId', 'sha16', 'firstName', 'householdName', 'zoneId', 'instanced'}} — foyers S4T.
    homes : {playerId: {'zoneId', 'occupiedLocally'}} — maison résolue dans cette partie (home_lots).
    outfits : {playerId: sha256} — apparence dont la tenue portée a déjà été appliquée au Sim présent.
    """
    # Le domicile d'une copie déjà enregistrée ne dépend pas d'une nouvelle
    # publication d'apparence en mémoire (absente après déconnexion/redémarrage).
    known_residents = set(ghosts).intersection(homes).intersection(names or {})
    for player_id in sorted(set(players).union(known_residents)):
        player = players.get(player_id)
        name = display_name(player.get('name') if player else (names or {}).get(player_id))
        ghost = ghosts.get(player_id)
        if ghost is None:
            if len(ghosts) >= MAX_GHOSTS:
                continue
            return ('create', player_id, {'name': name, 'sha256': player['sha256'], 'data': player['data']})
        if player is not None and ghost['sha16'] != player['sha256'][:16]:
            if not ghost['instanced']:
                return ('appearance', player_id, {'householdId': ghost['householdId'], 'sha256': player['sha256'],
                                                  'data': player['data']})
            # Sim présent sur le terrain : l'apparence complète attendra son départ, mais la tenue
            # portée (pyjama, maillot, sport…) change tout de suite.
            if ghost['sha16'] != PENDING and (outfits or {}).get(player_id) != player['sha256']:
                return ('outfit', player_id, {'householdId': ghost['householdId'], 'sha256': player['sha256'],
                                              'data': player['data']})
        if ghost['sha16'] == PENDING:
            # Apparence jamais appliquée : ni renommage ni emménagement de ce clone.
            continue
        if ghost['firstName'] != name or ghost['householdName'] != name + SUFFIX:
            return ('rename', player_id, {'householdId': ghost['householdId'], 'name': name})
        home = homes.get(player_id)
        zone_id = home.get('zoneId') if home else None
        if zone_id and str(ghost.get('zoneId') or '') != zone_id:
            if home.get('occupiedLocally') is False:
                return ('move', player_id, {'householdId': ghost['householdId'], 'zoneId': zone_id})
            if home.get('replaceableNpc') is True:
                return ('move', player_id, {'householdId':ghost['householdId'], 'zoneId':zone_id, 'allowNpc':True})
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


def _id64(value):
    return type(value) is int and 0 < value < 2**64


def residence_context(guid, slot_id, active_id):
    """Identité du journal de résidence : foyer actif obligatoire, GUID ou numéro de slot suffit.

    Une partie jamais enregistrée a souvent slot_id 0 : l'ancienne exigence des deux bloquait
    tout remplacement de PNJ. Le format reste celui d'avant quand les deux sont valides.
    """
    if not _id64(active_id) or not (_id64(guid) or _id64(slot_id)):
        raise LookupError('residence_save_identity')
    return ':'.join(str(v if _id64(v) else 0) for v in (guid, slot_id, active_id))


class RemoteHouseholds:
    """Appelée par AutoLink après home_lots et appearance_sync. Jamais d'exception vers le jeu."""

    def __init__(self, adapter=None):
        self.adapter = adapter if adapter is not None else GameHouseholds()
        self.attempts = {}
        self.failed_actions = {}
        self.failure_reasons = {}
        self.blocked_homes = {}
        self.cooldown = 0
        self.last = None
        self.ghosts = {}
        self.outfits = {}

    def reset(self):
        # Nouvelle zone : réessayer les déménagements avec une nouvelle lecture native.
        # Les créations/apparences ratées gardent leur budget pour éviter les doublons.
        for player_id, action in tuple(self.failed_actions.items()):
            if action[0] == 'move':
                self.attempts.pop(player_id, None)
                self.failed_actions.pop(player_id, None)
                self.failure_reasons.pop(player_id, None)
        self.cooldown = 0
        self.ghosts = {}
        self.outfits = {}
        self.blocked_homes = {}
        if hasattr(self.adapter, 'clear_reservations'):
            self.adapter.clear_reservations()

    def clear_reservations(self):
        if hasattr(self.adapter, 'clear_reservations'):
            self.adapter.clear_reservations()

    def tick(self, character, names, appearances, homes):
        if hasattr(self.adapter, 'is_ready') and not self.adapter.is_ready():
            self.clear_reservations()
            return None
        if character is None or character.get('status') != 'bound':
            if hasattr(self.adapter, 'clear_reservations'):
                self.adapter.clear_reservations()
            return None
        if hasattr(self.adapter, 'sync_homes'):
            self.adapter.sync_homes(homes)
        if self.cooldown > 0:
            self.cooldown -= 1
            return None
        self.ghosts = self.adapter.scan()
        players = {pid: {'name': names.get(pid), 'sha256': a['sha256'], 'data': a['data']}
                   for pid, a in appearances.items()}
        homes_by_player = {h['playerId']: h for h in homes if isinstance(h, dict) and 'playerId' in h}
        self.blocked_homes = {}
        for pid, home in homes_by_player.items():
            ghost = self.ghosts.get(pid)
            if ghost is not None and str(ghost.get('zoneId') or '') != home.get('zoneId'):
                if home.get('zoneId') is None:
                    self.blocked_homes[pid] = 'zone_unresolved'
                elif home.get('occupiedLocally') is True and home.get('replaceableNpc') is not True:
                    self.blocked_homes[pid] = 'occupied_locally'
        action = None
        known_residents = set(self.ghosts).intersection(homes_by_player).intersection(names)
        for pid in sorted(set(players).union(known_residents)):
            candidate = plan({pid: players[pid]} if pid in players else {},
                             self.ghosts, {pid: homes_by_player[pid]} if pid in homes_by_player else {},
                             {pid: names[pid]} if pid in names else {},
                             {pid: self.outfits[pid]} if pid in self.outfits else {})
            if candidate is None:
                continue
            kind, _, args = candidate
            # Budget lié à l'opération précise : une nouvelle maison n'hérite pas d'un ancien échec.
            # Le nom seul ne réarme pas une création ayant échoué.
            key = (kind, args.get('householdId'), args.get('zoneId'), args.get('sha256'))
            if self.failed_actions.get(pid) != key:
                self.attempts.pop(pid, None)
            if self.attempts.get(pid, 0) < MAX_ATTEMPTS:
                action = candidate
                break
        if action is None:
            return None
        kind, player_id, args = action
        try:
            getattr(self.adapter, kind)(player_id, **args)
            if kind == 'outfit':
                self.outfits[player_id] = args['sha256']
            self.attempts.pop(player_id, None)
            self.failed_actions.pop(player_id, None)
            self.failure_reasons.pop(player_id, None)
            log('remote_household', kind + ':ok')
            self.last = kind
        except Exception as error:
            self.attempts[player_id] = self.attempts.get(player_id, 0) + 1
            self.failed_actions[player_id] = key
            known = {'occupied', 'zone', 'local_household', 'pending', 'outfit_missing',
                     'residence_save_identity', 'residence_already_backed_up',
                     'residence_move_unconfirmed', 'residence_release_unconfirmed',
                     'residence_rollback_unconfirmed'}
            reason = str(error)
            self.failure_reasons[player_id] = reason if reason in known else type(error).__name__
            if kind == 'outfit':
                # Tenue absente localement (nouvelle tenue CAS) : elle arrivera avec l'apparence complète au
                # départ du Sim. Un seul essai par empreinte : ni boucle ni blocage du renommage/emménagement.
                self.outfits[player_id] = args['sha256']
                self.attempts.pop(player_id, None)
                self.failed_actions.pop(player_id, None)
            self.cooldown = RETRY_TICKS
            log('remote_household', kind + ':' + type(error).__name__)
            self.last = kind + ':' + type(error).__name__
        return kind

    def status(self):
        return {'ghosts': {pid: {'zoneId': g.get('zoneId'), 'instanced': g.get('instanced')} for pid, g in self.ghosts.items()},
                'failed': sorted(pid for pid, n in self.attempts.items() if n >= MAX_ATTEMPTS),
                'blockedHomes': dict(self.blocked_homes),
                'failureReasons': dict(self.failure_reasons),
                'last': self.last, 'gameReadiness': 'NOT_TESTED_GAME'}


class GameHouseholds:
    """Adaptateur du jeu. Chaque méthode lève en cas d'échec ; RemoteHouseholds compte et espace les essais."""

    def is_ready(self):
        try:
            import services
            return getattr(services.current_zone(), 'is_zone_running', False) is True
        except Exception:
            return False

    def _residence_journal(self):
        import hashlib
        import os
        from pathlib import Path
        import services
        from .residence import ResidenceJournal
        persistence = services.get_persistence_service()
        context = residence_context(persistence.get_save_slot_proto_guid(),
                                    getattr(persistence.get_save_slot_proto_buff(), 'slot_id', None),
                                    services.active_household_id())
        key = hashlib.sha256(context.encode('ascii')).hexdigest()
        root = Path(os.environ.get('LOCALAPPDATA', str(Path.home()))) / 'S4Together' / 'residence-backups'
        return ResidenceJournal(root / (key + '.json'), context)

    def clear_reservations(self):
        guard = getattr(self, 'residence_guard', None)
        if guard is not None:
            guard.clear()

    def sync_homes(self, homes):
        if not self.is_ready():
            self.clear_reservations()
            return
        import services
        from sims.household import Household
        from .residence import ResidenceGuard
        guard = getattr(Household.move_into_zone, 's4t_residence_guard', None)
        if guard is None:
            guard = ResidenceGuard(active_household_id=services.active_household_id)
            Household.move_into_zone = guard.wrap(Household.move_into_zone)
            Household.set_household_lot_ownership = guard.wrap(Household.set_household_lot_ownership)
        self.residence_guard = guard
        guard.set_homes({home['playerId']:int(home['zoneId']) for home in homes
                         if home.get('zoneId') and str(home['zoneId']).isdigit()}, clear_npc=True)
        try:
            # Partie liée : seules les maisons des joueurs sont habitées ; aucun PNJ n'est rendu.
            self._clear_npc_home(self._residence_journal())
        except Exception as error:
            # Une ligne par changement d'erreur, pas une par tick.
            code = type(error).__name__
            if getattr(self, '_restore_error', None) != code:
                self._restore_error = code
                log('residence_restore', type(error).__name__)
            return
        self._restore_error = None

    def _clear_npc_home(self, journal):
        """Libère au plus une maison PNJ non jouée par appel ; jamais un échec déjà vu.

        Terrain chargé d'abord (_clear_current_lot) : les Sims du PNJ qui y habite quittent le terrain, puis sa
        maison est libérée quand aucun n'est plus instancié. Ensuite seulement les autres terrains.
        """
        import services
        from .residence import replaceable
        current = getattr(services, 'current_zone_id', lambda: None)()
        if not current:
            return False
        failed = self.__dict__.setdefault('_evict_failed', set())
        active = services.active_household_id()
        manager = services.household_manager()
        zones = tuple(services.get_persistence_service().zone_proto_buffs_gen())
        here = next((zone for zone in zones if getattr(zone, 'zone_id', None) == current), None)
        result = self._clear_current_lot(journal, here, current, active, manager, failed)
        if result is not None:
            return result
        for zone in zones:
            household_id = getattr(zone, 'household_id', 0)
            if (not household_id or household_id == active or household_id in failed
                    or getattr(zone, 'active_plex', 0) or zone.zone_id == current):
                continue
            household = manager.get(household_id)
            if not replaceable(household, active, zone.zone_id, current):
                continue
            try:
                journal.evict(household, zone.zone_id)
            except Exception as error:
                failed.add(household_id)
                log('residence_evict', type(error).__name__)
                return False
            log('residence_evict', 'ok')
            return True
        return False

    def _leave_asked(self, current):
        """Appels depuis le dernier ordre de départ, par sim_id ; remis à zéro au changement de terrain."""
        if self.__dict__.get('_leave_zone') != current:
            self._leave_zone, self._leave_sims = current, {}
        return self._leave_sims

    def _clear_current_lot(self, journal, zone, current, active, manager, failed):
        """PNJ non joué domicilié sur le terrain chargé : départ de ses Sims, puis libération de sa maison.

        True/False : un foyer traité dans cet appel (succès/échec) ; None : rien à faire ici.
        API vérifiée dans le bytecode du jeu : SituationManager.make_sim_leave_now_must_run(sim),
        SimInfo.get_sim_instance(). Maison rachetée (terrain du foyer actif) : départ seulement, car
        Household.set_household_lot_ownership(0) appelle ZoneManager.clear_lot_ownership sur ce terrain et
        effacerait la propriété du foyer actif ; le home_zone_id périmé du PNJ n'est donc pas remis à zéro.
        """
        if zone is None or getattr(zone, 'active_plex', 0):
            return None
        import services
        from .residence import replaceable, resident_here
        owner = getattr(zone, 'household_id', 0)
        asked = self._leave_asked(current)
        for household in tuple(manager.values()):
            household_id = getattr(household, 'id', None)
            if not household_id or household_id == active or household_id in failed:
                continue
            infos = resident_here(household, active, current)
            # Foyer actif inconnu : rien n'est sûr, aucun Sim renvoyé.
            if infos is None or not active or owner not in (household_id, active):
                continue
            try:
                present = [(info, info.get_sim_instance()) for info in infos]
                present = [(info, sim) for info, sim in present if sim is not None]
                due = []
                for info, sim in present:
                    calls = asked.get(info.sim_id)
                    if calls is None or calls >= LEAVE_RETRY_CALLS:
                        due.append(sim)
                        asked[info.sim_id] = 0
                    else:
                        asked[info.sim_id] = calls + 1
                if due:
                    situations = services.get_zone_situation_manager()
                    for sim in due:
                        situations.make_sim_leave_now_must_run(sim)
                    log('residence_evict', 'leave')
                    return True
                if present or owner != household_id or not replaceable(household, active, current, current):
                    continue
                journal.evict(household, current)
            except Exception as error:
                failed.add(household_id)
                log('residence_evict', type(error).__name__)
                return False
            log('residence_evict', 'ok')
            return True
        return None

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

    def outfit(self, player_id, householdId, sha256, data):
        """Tenue portée seulement, Sim présent compris ; l'empreinte du foyer reste l'ancienne pour l'apparence complète."""
        from sims.outfits.outfit_enums import OutfitCategory
        from .remote_profile import current_outfit
        household = self._household(householdId)
        sim_info = self._sim_info(household)
        self._own(household, player_id)
        category, index = current_outfit(data)
        wanted = (OutfitCategory(category), index)
        if not sim_info.has_outfit(wanted):
            # Tenue créée depuis la dernière apparence complète : appliquée avec elle au départ du Sim.
            raise LookupError('outfit_missing')
        if sim_info.get_current_outfit() != wanted:
            sim_info.set_current_outfit(wanted)

    def rename(self, player_id, householdId, name):
        household = self._household(householdId)
        sim_info = self._sim_info(household)
        if self._own(household, player_id) == PENDING:
            raise RuntimeError('pending')
        sim_info.first_name = name
        sim_info.last_name = LAST_NAME
        household.name = name + SUFFIX
        sim_info.save_sim()

    def move(self, player_id, householdId, zoneId, allowNpc=False):
        import services
        household = self._household(householdId)
        if self._own(household, player_id) == PENDING:
            raise RuntimeError('pending')
        # Relu juste avant d'emménager : seul notre propre foyer distant peut déjà occuper le terrain.
        zone = services.get_persistence_service().get_zone_proto_buff(int(zoneId))
        if allowNpc and zone is not None and zone.household_id not in (0, household.id):
            from .residence import replaceable
            npc = services.household_manager().get(zone.household_id)
            current = getattr(services, 'current_zone_id', lambda: None)()
            if getattr(zone, 'active_plex', 0) or not replaceable(npc, services.active_household_id(), int(zoneId),
                                                                  current):
                raise PermissionError('occupied')
            journal = self._residence_journal()
            def occupant(zone_id):
                fresh = services.get_persistence_service().get_zone_proto_buff(zone_id)
                return fresh.household_id if fresh is not None else None
            def install():
                fresh = services.get_persistence_service().get_zone_proto_buff(int(zoneId))
                check_move(household.id, services.active_household_id(), fresh)
                household.move_into_zone(int(zoneId))
                if household.home_zone_id != int(zoneId):
                    raise RuntimeError('residence_move_unconfirmed')
                household.save_data()
            return journal.displace(npc, int(zoneId), player_id, household.id, install, occupant,
                                    active_id=services.active_household_id())
        check_move(household.id, services.active_household_id(), zone)
        household.move_into_zone(int(zoneId))
        if household.home_zone_id != int(zoneId):
            raise RuntimeError('residence_move_unconfirmed')
        household.save_data()
