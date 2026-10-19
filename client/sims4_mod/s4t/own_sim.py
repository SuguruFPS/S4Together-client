"""Retour du joueur sur son propre Sim et sur son propre foyer ; jamais sur la copie S4T d'un autre joueur.

Trois règles, appelées par CharacterSync :
- liaison ou chargement : si un autre Sim que ceux du personnage est actif, son Sim est resélectionné une fois ;
- tick lié : si la copie S4T d'un autre joueur est sélectionnée, retour sur son Sim (une tentative par sélection) ;
- copie S4T ou foyer d'un autre personnage chargé : bascule demandée vers le foyer du personnage, une fois par
  session et par paire personnage/foyer.

API vérifiée par désassemblage de simulation.zip 1.128 (Python 3.7.9, marshal + dis) :
services.get_first_client() ; server/client.pyc : Client.active_sim_info (propriété), Client.selectable_sims
(propriété, SelectableSims.__iter__ rend des SimInfo), Client.set_active_sim_by_id(sim_id, consider_active_sim=True)
(False si déjà actif ou non sélectionnable) ; sims/sim_info.pyc : SimInfo.household, household_id, zone_id.
Bascule de foyer : même suite que interactions/picker/switch_active_household_control_picker_interaction.pyc
(_do_switch_operation) et interactions/utils/interaction_elements.pyc (SwitcActivehHouseholdElement._do_behavior) :
services.household_manager().get(id), Household.home_zone_id, services.get_persistence_service() puis
PersistenceService.get_zone_proto_buff(zone_id), is_save_locked(), get_sim_proto_buff(sim_id).household_name,
distributor/ops.pyc SwitchActiveHouseholdControl(sim_id, zone_id, household_id, household_name) et
distributor/system.pyc Distributor.instance().add_op_with_no_owner(op). Le client C++ exécute ensuite la bascule.
"""
import re

from .journal import log

_NATIVE = re.compile(r'^[0-9]{1,20}$')


def _native(value):
    return str(value) if type(value) is int and 0 < value < 10 ** 20 else None


def _ids(values):
    return [v for v in values if isinstance(v, str) and _NATIVE.match(v)] if isinstance(values, list) else []


def _is_copy(household):
    from .character_sync import _is_s4t_copy
    return household is not None and _is_s4t_copy(household)


def game_selection():
    """Sélection du jeu : {activeSimId, activeIsCopy, selectable} ou None sans client."""
    import services
    client = services.get_first_client()
    if client is None:
        return None
    info = client.active_sim_info
    selectable = [s for s in (_native(getattr(i, 'sim_id', None)) for i in client.selectable_sims) if s]
    active = _native(getattr(info, 'sim_id', None)) if info is not None else None
    return {'activeSimId': active, 'activeIsCopy': info is not None and _is_copy(getattr(info, 'household', None)),
            'selectable': selectable}


def game_select(sim_id):
    """Sélectionne ce Sim comme la commande native `set_active_sim` ; vrai si la sélection a changé."""
    import services
    if not isinstance(sim_id, str) or not _NATIVE.match(sim_id):
        return False
    client = services.get_first_client()
    return client is not None and client.set_active_sim_by_id(int(sim_id)) is True


def game_switch_household(household_id, sim_ids):
    """Demande au jeu de jouer ce foyer ; 'requested' ou la raison du refus. Mêmes contrôles que le jeu."""
    import services
    from distributor.ops import SwitchActiveHouseholdControl
    from distributor.system import Distributor
    if not isinstance(household_id, str) or not _NATIVE.match(household_id):
        return 'household_missing'
    target = int(household_id)
    household = services.household_manager().get(target)
    if household is None:
        return 'household_missing'
    if _is_copy(household):
        return 'household_copy'
    if not household.home_zone_id:
        return 'no_home'
    sims = services.sim_info_manager()
    info = None
    for sim_id in _ids(sim_ids):
        candidate = sims.get(int(sim_id))
        if candidate is not None and candidate.household_id == target and candidate.zone_id:
            info = candidate
            break
    if info is None:
        return 'sim_missing'
    persistence = services.get_persistence_service()
    if persistence.get_zone_proto_buff(info.zone_id) is None:
        return 'zone_missing'
    if persistence.is_save_locked():
        return 'save_locked'
    proto = persistence.get_sim_proto_buff(info.sim_id)
    if proto is None:
        return 'sim_missing'
    op = SwitchActiveHouseholdControl(sim_id=info.sim_id, zone_id=info.zone_id, household_id=info.household_id,
                                      household_name=proto.household_name)
    Distributor.instance().add_op_with_no_owner(op)
    return 'requested'


class OwnSimGuard:
    """Logique pure ; les fonctions du jeu sont injectées. Jamais d'exception vers l'appelant."""

    def __init__(self, selection=game_selection, select=game_select, switch=game_switch_household):
        self.selection = selection
        self.select = select
        self.switch = switch
        # Bascules déjà demandées dans cette session de jeu. Survit à reset() : jamais de boucle de chargement.
        self.switched = set()
        # Chargements de zone : la bascule hors d'une copie S4T est redemandée à chacun (une fois par chargement).
        self.loads = 0
        self.reset()

    def reset(self):
        """Nouvelle zone ou reconnexion."""
        self.loads += 1
        self.load_pending = False
        self.failed_on = None  # Sim actif pour lequel le retour a échoué : pas de nouvel essai tant qu'il reste actif.
        self.last_note = None

    def on_bound(self, character):
        """Personnage lié ou repris : retour sur son Sim une fois pour ce chargement."""
        self.load_pending = True
        return self._check(character)

    def on_tick(self, character):
        return self._check(character)

    def _note(self, code):
        # Une ligne par transition.
        if code != self.last_note:
            self.last_note = code
            log('own_sim', code)

    def _check(self, character):
        own = _ids(((character or {}).get('binding') or {}).get('simIds'))
        if not own:
            self.load_pending = False
            return None
        try:
            view = self.selection()
        except Exception as error:
            self._note('read:' + type(error).__name__)
            return None
        if not isinstance(view, dict):
            return None
        loading, self.load_pending = self.load_pending, False
        active = view.get('activeSimId')
        if active in own:
            self.failed_on = None
            return None
        if not loading and view.get('activeIsCopy') is not True:
            return None  # Un autre Sim de son foyer, choisi par le joueur : libre.
        if active is not None and active == self.failed_on:
            return None
        selectable = view.get('selectable') if isinstance(view.get('selectable'), list) else []
        target = next((s for s in own if s in selectable), None)
        if target is None:
            self._note('not_selectable')
            return None
        try:
            done = self.select(target) is True
        except Exception as error:
            self._note('select:' + type(error).__name__)
            done = False
        if not done:
            self.failed_on = active
            self._note('reselect_failed')
            return None
        self.failed_on = None
        self._note('reselected_load' if loading else 'left_copy')
        self.last_note = None
        return 'reselected'

    def switch_household(self, character, every_load=False):
        """Bascule vers le foyer du personnage : 'requested', 'already_requested' ou la raison du refus.

        every_load : copie S4T d'un autre joueur chargée ; la bascule est redemandée à chaque chargement de zone,
        pas une seule fois par session, pour qu'on ne puisse pas rester sur la copie en la rechargeant.
        """
        binding = (character or {}).get('binding') or {}
        household, sims = binding.get('gameHouseholdId'), _ids(binding.get('simIds'))
        if not isinstance(household, str) or not _NATIVE.match(household) or not sims:
            return 'no_binding'
        key = str(character.get('characterId')) + '/' + household
        if every_load:
            key += '/load' + str(self.loads)
        if key in self.switched:
            return 'already_requested'
        self.switched.add(key)
        try:
            result = self.switch(household, sims)
        except Exception as error:
            log('own_household', 'switch:' + type(error).__name__)
            return 'error'
        result = result if isinstance(result, str) else 'error'
        log('own_household', 'switch_' + result)
        return result
