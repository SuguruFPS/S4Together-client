"""Proxies des joueurs : filtrage social permanent, hors ligne ou en ligne (#163).

Un foyer « Nom (S4T) » dont le joueur n'est pas connecté au serveur est un PNJ ordinaire pour le jeu : il se promène,
téléphone, invite. Tant que son joueur est absent (ou que nous sommes nous-mêmes déconnectés du serveur) :
- ses Sims sont retirés de tous les filtres de Sims du jeu (passants, invités, expéditeurs d'appels) ;
- un appel ou une invitation dont il est l'expéditeur est annulé au moment de s'afficher ;
- s'il est présent ailleurs que sur son terrain, il est renvoyé chez lui.
Chez lui, il reste visible : le jeu fait apparaître les habitants d'un terrain sans passer par les filtres.

Chaque joueur est traité séparément : le retour lève le renvoi hors ligne, pas le filtrage social du proxy.
Ce filtrage ne désactive pas toutes les voies d'autonomie du moteur. Les surcharges de drama.run et les
acceptations d'anciens dialogues doivent encore être étudiées ; aucune protection universelle revendiquée.

API vérifiée par désassemblage 1.128 (lecture seule), jamais exécutée en jeu :
services.sim_filter_service().add_sim_id_to_global_blacklist(sim_id, reason) / remove_… ; le service tient
_global_blacklist = {sim_id: [motifs]} (doublons permis, retrait d'un motif absent = erreur journalisée par le jeu) :
notre motif est donc lu avant chaque ajout ou retrait, et un motif posé par le jeu n'est jamais retiré ;
filters.sim_filter_service.SimFilterGlobalBlacklistReason (PHONE_CALL, SIM_INFO_BEING_REMOVED, MISSING_PET,
RABBIT_HOLE) ; drama_scheduler.drama_node.BaseDramaNode.run() rend DramaNodeRunOutcome.FAILURE quand ses tests
échouent et DramaScheduleService._run_node nettoie alors le nœud ; BaseDramaNode.get_sender_sim_info() ;
services.get_zone_situation_manager().make_sim_leave_now_must_run(sim) ; SimInfo.get_sim_instance(),
SimInfo.sim_id, Household.sim_info_gen(), Household.home_zone_id, services.current_zone_id().

Non couvert : le message « pas disponible » quand le joueur local téléphone lui-même à un Sim hors ligne. Le Sim
est absent des listes construites par filtre ; les listes construites autrement ne sont pas modifiées.
NOT_TESTED_GAME.
"""
from .journal import log
from .remote_household import parse_description

# tick() est appelé toutes les CHECK_TICKS secondes par AutoLink.
CHECK_TICKS = 5
# Une coupure réseau courte ne renvoie personne : le Sim n'est prié de rentrer qu'après ce nombre de contrôles hors ligne.
HOME_GRACE_CALLS = 6
# Puis délai avant de le redemander au même Sim.
LEAVE_RETRY_CALLS = 12


def offline_households(ghosts, online):
    """{householdId: playerId} des foyers S4T dont le joueur n'est pas connecté. Python pur."""
    return {ghost['householdId']: player_id for player_id, ghost in ghosts.items()
            if player_id not in online and ghost.get('householdId')}


class OfflineGuard:
    """Appelée par AutoLink dans toute partie chargée, connectée au serveur ou non. Jamais d'exception vers le jeu."""

    def __init__(self, adapter=None):
        self.adapter = adapter if adapter is not None else GameOffline()
        self.blocked = {}
        self.asked = {}
        self.last = None
        self.offline_players = set()

    def reset(self):
        # Nouvelle zone : tout est relu au prochain contrôle (l'adaptateur ne pose jamais deux fois notre motif).
        self.blocked = {}
        self.asked = {}
        self.offline_players = set()

    def is_blocked(self, sim_id):
        return sim_id in self.blocked

    def tick(self, ghosts, online):
        """ghosts : foyers S4T lus dans la partie ; online : playerId connectés (vide si nous sommes déconnectés)."""
        try:
            self.adapter.install(self.is_blocked)
        except Exception as error:
            log('offline_guard', 'install:' + type(error).__name__)
        wanted = {}
        self.offline_players = set(offline_households(ghosts, online).values())
        # Un proxy n'est jamais un PNJ autonome, même quand son propriétaire joue ailleurs.
        for player_id, ghost in ghosts.items():
            household_id = ghost.get('householdId')
            if not household_id:
                continue
            try:
                for sim_id in self.adapter.sim_ids(household_id):
                    wanted[sim_id] = player_id
            except Exception as error:
                log('offline_guard', 'household:' + type(error).__name__)
        for sim_id in [sim_id for sim_id in self.blocked if sim_id not in wanted]:
            self._step(self.adapter.unblock, sim_id, 'unblock')
            del self.blocked[sim_id]
            self.asked.pop(sim_id, None)
        for sim_id, player_id in wanted.items():
            if sim_id not in self.blocked and self._step(self.adapter.block, sim_id, 'block'):
                self.blocked[sim_id] = player_id
            neutralize = getattr(self.adapter, 'neutralize', None)
            if neutralize is not None:
                self._step(neutralize, sim_id, 'proxy_motives')
        sent = 0
        for sim_id in self.blocked:
            if self.blocked[sim_id] not in self.offline_players:
                self.asked.pop(sim_id, None)
                continue  # Les instances en ligne restent pilotables par RemoteMotion.
            if sim_id not in self.asked:
                self.asked[sim_id] = HOME_GRACE_CALLS
            if self.asked.get(sim_id, 0) > 0:
                self.asked[sim_id] -= 1
            elif self._step(self.adapter.send_home, sim_id, 'send_home'):
                self.asked[sim_id] = LEAVE_RETRY_CALLS
                sent += 1
        self.last = {'blocked': len(self.blocked), 'sentHome': sent}
        return self.last

    def _step(self, action, sim_id, name):
        try:
            return bool(action(sim_id))
        except Exception as error:
            log('offline_guard', name + ':' + type(error).__name__)
            return False

    def status(self):
        return {'offlinePlayers': sorted(self.offline_players), 'blockedSims': len(self.blocked), 'last': self.last,
                'gameReadiness': 'NOT_TESTED_GAME'}


def veto(is_blocked, original, failure):
    """Enveloppe de BaseDramaNode.run : un nœud dont l'expéditeur est bloqué échoue comme un test natif raté."""
    def run(self, *args, **kwargs):
        try:
            sender = self.get_sender_sim_info()
            # L'identite deja connue reste protegee meme si le foyer natif est
            # temporairement illisible pendant un voyage/rechargement de zone.
            blocked = False
            try:
                blocked = sender is not None and is_blocked(getattr(sender, 'sim_id', None))
            except Exception as error:
                log('offline_guard', 'veto_identity:' + type(error).__name__)
            # Lecture de l'identité proxy dès le premier événement, avant le premier tick et après voyage.
            if not blocked:
                household = getattr(sender, 'household', None)
                blocked = parse_description(getattr(household, 'description', None)) is not None
        except Exception as error:
            log('offline_guard', 'veto:' + type(error).__name__)
            blocked = False
        if blocked:
            log('offline_guard', 'contact_canceled')
            return failure
        return original(self, *args, **kwargs)
    run.s4t_original = original
    return run


def veto_autonomy(original, failure=False):
    """Le proxy distant non sélectionnable ne choisit jamais sa propre prochaine interaction autonome."""
    def test(component, *args, **kwargs):
        try:
            info = component.owner.sim_info
            if (info.is_selectable is False
                    and parse_description(getattr(info.household, 'description', None)) is not None):
                return failure
        except (AttributeError, TypeError):
            pass
        return original(component, *args, **kwargs)
    test.s4t_original = original
    return test


def veto_invitation(is_blocked, original):
    """Refuse le clic/creation d'une invitation PNJ issue d'un proxy, avant loots ou voyage.

    Ne termine pas le noeud via complete_node : ses callbacks de completion
    pourraient eux-memes produire des effets. Aucun nettoyage force : un ancien
    noeud peut rester actif jusqu'au nettoyage natif du service, non prouve ici.
    Pas de suppression d'entite ni modification de foyer.
    """
    guarded = veto(is_blocked, original, None)
    return guarded


class GameOffline:
    """Adaptateur du jeu. Chaque méthode peut lever ; OfflineGuard journalise et continue."""

    def __init__(self):
        from .proxy_motives import ProxyMotiveGuard
        self.motives = ProxyMotiveGuard()

    def neutralize(self, sim_id):
        import services
        from autonomy.autonomy_modifier import AutonomyModifier
        info = services.sim_info_manager().get(sim_id)
        sim = info.get_sim_instance() if info is not None else None
        return sim is not None and self.motives.update(sim, services.active_household_id(), AutonomyModifier)

    def install(self, is_blocked):
        from drama_scheduler import drama_node
        node = drama_node.BaseDramaNode
        if getattr(node.run, 's4t_original', None) is None:
            node.run = veto(is_blocked, node.run, drama_node.DramaNodeRunOutcome.FAILURE)
        try:
            from drama_scheduler.npc_invite_situation_drama_node import NPCInviteSituationDramaNode
            for method in ('_handle_dialog', '_handle_picker_dialog', '_create_situation'):
                original = getattr(NPCInviteSituationDramaNode, method)
                if getattr(original, 's4t_original', None) is None:
                    setattr(NPCInviteSituationDramaNode, method, veto_invitation(is_blocked, original))
        except (ImportError, AttributeError) as error:
            log('offline_guard', 'invitation_hook:' + type(error).__name__)
        try:
            from autonomy.autonomy_component import AutonomyComponent
            original = AutonomyComponent._test_full_autonomy
            if getattr(original, 's4t_original', None) is None:
                AutonomyComponent._test_full_autonomy = veto_autonomy(original)
        except (ImportError, AttributeError) as error:
            log('offline_guard', 'autonomy_hook:' + type(error).__name__)
        try:
            from autonomy.autonomy_component import AutonomyComponent
            from event_testing.results import EnqueueResult
            original = AutonomyComponent.run_subaction_autonomy
            if getattr(original, 's4t_original', None) is None:
                AutonomyComponent.run_subaction_autonomy = veto_autonomy(original, EnqueueResult.NONE)
        except (ImportError, AttributeError) as error:
            log('offline_guard', 'subautonomy_hook:' + type(error).__name__)

    def _reason(self):
        from filters.sim_filter_service import SimFilterGlobalBlacklistReason
        # Motif que le jeu ne pose jamais sur un Sim humain : il ne peut pas être confondu avec un blocage natif.
        return SimFilterGlobalBlacklistReason.MISSING_PET

    def sim_ids(self, household_id):
        import services
        household = services.household_manager().get(int(household_id))
        if household is None or household.id == services.active_household_id():
            return []
        return [sim_info.sim_id for sim_info in household.sim_info_gen()]

    def block(self, sim_id):
        import services
        service, reason = services.sim_filter_service(), self._reason()
        if reason not in service._global_blacklist.get(sim_id, ()):
            service.add_sim_id_to_global_blacklist(sim_id, reason)
        return True

    def unblock(self, sim_id):
        import services
        if self.motives.records:
            info = services.sim_info_manager().get(sim_id)
            sim = info.get_sim_instance() if info is not None else None
            if sim is not None:
                self.motives.release(sim)
        service, reason = services.sim_filter_service(), self._reason()
        if reason in service._global_blacklist.get(sim_id, ()):
            service.remove_sim_id_from_global_blacklist(sim_id, reason)
        return True

    def send_home(self, sim_id):
        """True si le Sim, présent ailleurs que chez lui, vient de recevoir l'ordre de partir."""
        import services
        sim_info = services.sim_info_manager().get(sim_id)
        sim = sim_info.get_sim_instance() if sim_info is not None else None
        if sim is None or sim_info.household.home_zone_id == services.current_zone_id():
            return False
        services.get_zone_situation_manager().make_sim_leave_now_must_run(sim)
        return True
