"""Preuve de chargement et raccordement au chargement de zone, sans aucun dialogue automatique.

Hook vérifié par désassemblage de simulation.zip 1.128 : Zone.on_loading_screen_animation_finished(self)
a deux appelants, les commandes natives `zone.loading_screen_animation_finished`
(server_commands/zone_commands.py) et `clock.restore_saved_clock_speed` (server_commands/clock_commands.py).
Le même chargement peut donc passer deux fois ici : un seul passage est traité par instance de zone.
"""
from .journal import log

_SEEN = '_s4t_zone_loaded'


def _first_pass(zone):
    """Vrai au premier passage pour cette instance de zone (le jeu en crée une par chargement)."""
    if getattr(zone, _SEEN, False):
        return False
    try:
        setattr(zone, _SEEN, True)
    except (AttributeError, TypeError) as error:
        # Marquage impossible : on_zone_loaded reste idempotent, un second passage ne double rien.
        log('zone_mark', type(error).__name__)
    return True


def _install_teardown(zone_class, commands):
    """Présence (#156) : game.leave au déchargement de zone. Installé seulement si le jeu expose le hook.

    Zone.on_teardown(self, client) est présent dans simulation.zip 1.128 (désassemblage) ; son appel réel
    au voyage et au retour au menu n'est pas vérifié en jeu. Sans lui, le serveur retire la présence à la
    déconnexion et le prochain game.join remplace l'ancienne zone.
    """
    method = getattr(zone_class, 'on_teardown', None)
    if method is None:
        log('bootstrap', 'teardown_unavailable')
        return False
    if getattr(method, '_s4t_wrapper', False):
        return True
    def teardown(self, *args, **kwargs):
        try:
            if commands._auto is not None:
                commands._auto.on_zone_teardown()
        except Exception as error:
            log('zone_teardown', type(error).__name__)
        return method(self, *args, **kwargs)
    teardown._s4t_wrapper = True
    zone_class.on_teardown = teardown
    return True


def _install_save(commands, module=None):
    """Sauvegarde réussie : checkpoint personnage et domicile envoyés tout de suite au serveur.

    Vérifié par désassemblage de simulation.zip 1.128 : toutes les sauvegardes (commandes save_game,
    override_save_slot, save_to_new_slot, autosave, et save_to_scratch_slot_gen au voyage ou à la sortie)
    passent par le générateur PersistenceService.save_game_gen(self, timeline, save_game_data,
    send_save_message, check_cooldown, ignore_callback), qui renvoie un SaveGameResult. Appel réel non vérifié
    en jeu. Sans ce hook, le checkpoint périodique et celui du déchargement de zone restent seuls.
    """
    try:
        if module is None:
            from services import persistence_service as module
        service_class = module.PersistenceService
        success = module.SaveGameResult.SUCCESS
        method = service_class.save_game_gen
    except Exception as error:
        log('bootstrap', 'save_unavailable:' + type(error).__name__)
        return False
    if getattr(method, '_s4t_wrapper', False):
        return True
    def save_game_gen(self, *args, **kwargs):
        result = yield from method(self, *args, **kwargs)
        try:
            if result == success and commands._auto is not None:
                commands._auto.on_game_saved()
        except Exception as error:
            log('game_saved', type(error).__name__)
        return result
    save_game_gen._s4t_wrapper = True
    service_class.save_game_gen = save_game_gen
    return True


def _install_proxy_guard():
    """Veto des expéditeurs S4T avant le premier chargement, sans état de présence ni réseau."""
    try:
        from .offline_guard import GameOffline
        GameOffline().install(lambda _sim_id: False)
        return True
    except Exception as error:
        log('bootstrap', 'proxy_guard_unavailable:' + type(error).__name__)
        return False


def install():
    log('bootstrap', 'started')
    try:
        from . import commands
        if commands.game_commands is None:
            log('bootstrap', 'game_commands_unavailable')
            return False
        _install_proxy_guard()
        import zone
        method = zone.Zone.on_loading_screen_animation_finished
        if getattr(method, '_s4t_wrapper', False):
            return True
        def loaded(self):
            result = method(self)
            try:
                if _first_pass(self):
                    # Launcher actif : connexion via son pont. Sinon le mod reste passif : aucun dialogue,
                    # aucune connexion, aucune alarme. s4t.menu et s4t.diagnostic restent à la demande.
                    active = commands.auto_link().on_zone_loaded()
                    log('zone_loaded', 'launcher' if active else 'passive')
            except Exception as error:
                log('zone_loaded', type(error).__name__)
            return result
        loaded._s4t_wrapper = True
        zone.Zone.on_loading_screen_animation_finished = loaded
        _install_teardown(zone.Zone, commands)
        _install_save(commands)
        log('bootstrap', 'registered')
        return True
    except Exception as error:
        log('bootstrap', type(error).__name__)
        return False
