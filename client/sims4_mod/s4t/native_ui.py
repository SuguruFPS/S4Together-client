"""Dialogues natifs sur le thread du jeu ; aucune ressource EA embarquée.

Aucun dialogue n'est ouvert automatiquement : ils répondent tous à une commande s4t.* tapée par le joueur.
"""
from .journal import log

def dialog(title, text, on_connect=None):
    try:
        import services
        from sims4.localization import LocalizationHelperTuning
        from ui.ui_dialog import UiDialogOk, UiDialogOkCancel
        owner = services.get_active_sim()
        if owner is None:
            raise RuntimeError('Charger un foyer en mode Vie')
        localize = LocalizationHelperTuning.get_raw_text
        factory = UiDialogOkCancel if on_connect else UiDialogOk
        value = factory.TunableFactory().default(owner,
            title=lambda **kwargs: localize(title), text=lambda **kwargs: localize(text),
            **({'ok_text': lambda **kwargs: localize('Se connecter'),
                'cancel_text': lambda **kwargs: localize('Fermer')} if on_connect else {}))
        def response(result):
            if result.accepted:
                on_connect()
        value.show_dialog(**({'on_response': response} if on_connect else {}))
        log('native_dialog')
        return True
    except (ImportError, AttributeError, TypeError, RuntimeError) as error:
        log('native_dialog', type(error).__name__)
        return False

def connection_form(settings, on_submit):
    try:
        import services
        from urllib.parse import urlsplit
        from sims4.localization import LocalizationHelperTuning
        from ui.ui_dialog_generic import UiDialogTextInputOkCancel
        from ui.ui_text_input import UiTextInput
        owner = services.get_active_sim()
        if owner is None:
            raise RuntimeError('Charger un foyer en mode Vie')
        current = urlsplit(settings.get('endpoint', ''))
        address = settings.get('endpoint', '') if current.hostname not in ('localhost', '127.0.0.1') else ''
        localize = LocalizationHelperTuning.get_raw_text
        fields = {}
        for key, label, initial in [('address', 'Adresse IP LAN ou adresse wss:// du serveur', address),
                ('port', 'Port du jeu', str(current.port or 27015)),
                ('name', 'Nom du joueur', settings.get('name', 'Joueur Sims')),
                ('password', 'Mot de passe (non sauvegardé)', '')]:
            fields[key] = UiTextInput.TunableFactory().default(
                title=lambda value=label, **kwargs: localize(value),
                initial_value=lambda value=initial, **kwargs: localize(value), check_profanity=False)
        value = UiDialogTextInputOkCancel.TunableFactory().default(owner,
            title=lambda **kwargs: localize('S4Together — Connexion'),
            text=lambda **kwargs: localize('Connexion réseau de test. Aucune sauvegarde Sims n’est modifiée.'),
            text_inputs=fields)
        def response(result):
            if not result.accepted:
                return
            try:
                answers = result.text_input_responses
                on_submit(answers.get('address', ''), answers.get('port', ''),
                    answers.get('name', ''), answers.get('password', ''))
            except (ValueError, RuntimeError, OSError, ImportError) as error:
                log('configure', type(error).__name__)
                dialog('S4Together — Erreur', 'Adresse, port ou connexion refusés. Consulter s4t.diagnostic et le journal exportable.')
        value.show_dialog(on_response=response)
        return True
    except (ImportError, AttributeError, TypeError, RuntimeError) as error:
        log('connection_form', type(error).__name__)
        return False
