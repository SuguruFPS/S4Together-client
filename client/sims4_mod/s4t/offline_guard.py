"""Sims des joueurs hors ligne : ils restent chez eux et ne contactent personne (13/10/2026).

Un foyer « Nom (S4T) » dont le joueur n'est pas connecté au serveur est un PNJ ordinaire pour le jeu : il se promène,
téléphone, invite. Tant que son joueur est absent (ou que nous sommes nous-mêmes déconnectés du serveur) :
- ses Sims sont retirés de tous les filtres de Sims du jeu (passants, invités, expéditeurs d'appels) ;
- un appel ou une invitation dont il est l'expéditeur est annulé au moment de s'afficher ;
- s'il est présent ailleurs que sur son terrain, il est renvoyé chez lui.
Chez lui, il reste visible : le jeu fait apparaître les habitants d'un terrain sans passer par les filtres.

Chaque joueur est traité séparément (aucun état global) : le retour d'un joueur ne lève que son propre blocage.

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

    def reset(self):
        # Nouvelle zone : tout est relu au prochain contrôle (l'adaptateur ne pose jamais deux fois notre motif).
        self.blocked = {}
        self.asked = {}

    def is_blocked(self, sim_id):
        return sim_id in self.blocked

    def tick(self, ghosts, online):
        """ghosts : foyers S4T lus dans la partie ; online : playerId connectés (vide si nous sommes déconnectés)."""
        try:
            self.adapter.install(self.is_blocked)
        except Exception as error:
            log('offline_guard', 'install:' + type(error).__name__)
        wanted = {}
        for household_id, player_id in offline_households(ghosts, online).items():
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
                self.asked[sim_id] = HOME_GRACE_CALLS
        sent = 0
        for sim_id in self.blocked:
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
        return {'offlinePlayers': sorted(set(self.blocked.values())), 'blockedSims': len(self.blocked), 'last': self.last,
                'gameReadiness': 'NOT_TESTED_GAME'}


def veto(is_blocked, original, failure):
    """Enveloppe de BaseDramaNode.run : un nœud dont l'expéditeur est bloqué échoue comme un test natif raté."""
    def run(self, *args, **kwargs):
        try:
            sender = self.get_sender_sim_info()
            blocked = sender is not None and is_blocked(sender.sim_id)
        except Exception as error:
            log('offline_guard', 'veto:' + type(error).__name__)
            blocked = False
        if blocked:
            log('offline_guard', 'contact_canceled')
            return failure
        return original(self, *args, **kwargs)
    run.s4t_original = original
    return run


class GameOffline:
    """Adaptateur du jeu. Chaque méthode peut lever ; OfflineGuard journalise et continue."""

    def install(self, is_blocked):
        from drama_scheduler import drama_node
        node = drama_node.BaseDramaNode
        if getattr(node.run, 's4t_original', None) is None:
            node.run = veto(is_blocked, node.run, drama_node.DramaNodeRunOutcome.FAILURE)

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
