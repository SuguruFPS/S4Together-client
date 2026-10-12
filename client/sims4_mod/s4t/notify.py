"""Notification native en jeu (coin de l'écran), sans tuning : texte brut localisé.

API vérifiée par désassemblage : ui.ui_dialog.UiDialog hérite de HasTunableFactory/AutoFactoryInit
(TunableFactory(cls) dans sims4.tuning.tunable), UiDialog.__init__(owner, resolver, target_sim_id),
UiDialogNotification.show_dialog(self, **kwargs), LocalizationHelperTuning.get_raw_text(cls, text).
"""
from .journal import log


def game_notify(title, text):
    """Affiche une notification ; faux (et journalisé) si le jeu la refuse, jamais d'exception."""
    try:
        import services
        from sims4.localization import LocalizationHelperTuning
        from ui.ui_dialog_notification import UiDialogNotification
        owner = services.active_sim_info()
        dialog = UiDialogNotification.TunableFactory().default(
            owner,
            text=lambda *_, **__: LocalizationHelperTuning.get_raw_text(text),
            title=lambda *_, **__: LocalizationHelperTuning.get_raw_text(title))
        dialog.show_dialog()
        return True
    except Exception as error:
        log('notify', type(error).__name__)
        return False
