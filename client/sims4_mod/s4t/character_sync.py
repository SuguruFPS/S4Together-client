"""Personnage serveur (T3) relié au foyer actif du jeu : reprise, liaison, fonds, relations PNJ, checkpoint.

Le serveur fait autorité (contrat `character.*` de shared/protocol/EXTENSIONS.md). Le mod observe le foyer
actif, applique au plus une fois par personnage ce que l'ACK demande, puis envoie des checkpoints.

API vérifiée par désassemblage de simulation.zip 1.128 :
services.active_household(), services.active_household_id() ; Household.funds (propriété -> _Funds), Household.sim_info_gen(),
Household.home_zone_id, Household.get_home_world_id() ; _Funds.money (propriété) ;
_Funds.add(amount, reason, sim, tags, count_as_earnings) et _Funds.try_remove(amount, reason, sim,
require_full_amount), sim=None géré par _update_money ; même raison que la commande native `money`
(Consts_pb2.TELEMETRY_MONEY_CHEAT) ; SimInfo.relationship_tracker (propriété) ;
RelationshipTracker.target_sim_gen() et destroy_relationship(target_sim_id, notify_client) ;
services.sim_info_manager().get(sim_id) (IndexedManager.get, None si absent) ; SimInfo.household (None si le
foyer est introuvable) ; Household.is_player_household, is_played_household, description, name (propriétés).
SimInfo.is_npc compare seulement le foyer du Sim au foyer actif : il ne distingue pas un PNJ du Sim d'un autre
foyer joué et n'est pas utilisé. Détail et limites : docs/NATIVE_OBJECTIVES.md.
"""
import json
import re
import time
import uuid

from . import solo_guard
from .journal import log
from .notify import game_notify
from .remote_household import PREFIX, SUFFIX

CHECKPOINT_TICKS = 15
MAX_FUNDS = 9999999
_UUID = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')
_NATIVE = re.compile(r'^[0-9]{1,20}$')
TITLE = 'S4Together'
# Refus après lesquels le même bind pourra être rejoué à la reprise suivante.
_TRANSIENT = ('storage_unavailable', 'request_limit', 'revision_conflict', 'error')
_UNREAD = object()
_SOLO_NEW = ("Le personnage serveur {} n'est pas encore lié au foyer chargé. Une sauvegarde nommée multijoueur "
             "ne suffit pas à établir cette liaison. Pour reprendre un Sim déjà lié, sélectionne sa carte dans le launcher. "
             "Pour créer ce nouveau personnage, choisis « Nouvelle partie », puis enregistre dans une sauvegarde distincte. "
             "Le foyer chargé reste inchangé.")
_SOLO_OTHER = ("Le foyer chargé ne correspond pas à la liaison du personnage serveur {}. "
               "Charge la sauvegarde multijoueur de ce personnage ou sélectionne le personnage correspondant dans le launcher. "
               "Le foyer chargé reste inchangé.")
_SOLO_BINDING = ("L'ancienne liaison de {} pointe vers une partie solo : elle est incorrecte et rien n'est restauré. "
                 "Recrée ce personnage dans le launcher, puis choisis « Nouvelle partie » dans une sauvegarde distincte.")
_SOLO_GUARD = ("Protection des parties solo illisible ou prévue pour un autre personnage : rien n'est modifié. "
               "Relance le jeu depuis le launcher.")


def _native(value):
    return str(value) if type(value) is int and 0 < value < 10 ** 20 else None


def _save_slot_id(services):
    """slot_id de la sauvegarde chargée (SaveSlotData champ 1, via PersistenceService.get_save_slot_proto_buff), ou None."""
    try:
        return _native(getattr(services.get_persistence_service().get_save_slot_proto_buff(), 'slot_id', None))
    except Exception:
        return None


def game_household():
    """Observation du foyer actif : {householdId, simIds, funds, homeLotId, gameWorld, saveSlotId} ou None."""
    import services
    household = services.active_household()
    if household is None:
        return None
    household_id = _native(services.active_household_id())
    infos = list(household.sim_info_gen())
    sim_ids = [s for s in (_native(info.sim_id) for info in infos) if s]
    if household_id is None or not sim_ids:
        return None
    world = household.get_home_world_id()
    observed = {'householdId': household_id, 'simIds': sim_ids[:8], 'funds': household.funds.money,
            'homeLotId': _native(household.home_zone_id), 'gameWorld': _native(world),
            'saveSlotId': _save_slot_id(services)}
    pets = [_native(info.sim_id) for info in infos if getattr(info, 'is_pet', None) is True]
    if pets and all(pets):
        observed['petSimIds'] = pets
    return observed


def game_set_funds(target):
    """Amène les fonds du foyer actif à `target` (comme la commande native `money`)."""
    import services
    from protocolbuffers import Consts_pb2
    household = services.active_household()
    if household is None:
        return False
    funds = household.funds
    delta = target - funds.money
    if delta > 0:
        funds.add(delta, Consts_pb2.TELEMETRY_MONEY_CHEAT, None, count_as_earnings=False)
    elif delta < 0:
        funds.try_remove(-delta, Consts_pb2.TELEMETRY_MONEY_CHEAT, None, require_full_amount=False)
    return True


def _relation_kind(genealogy, target, sims):
    """'npc' seulement pour un PNJ identifié ; sinon la raison de garder la relation.

    'family' : généalogie. 's4t' : foyer qui porte une trace S4T (clone finalisé, en attente ou abîmé).
    'player' : foyer joueur ou joué d'après le jeu. 'unknown' : cible invalide, Sim ou foyer introuvable,
    lecture en échec ou valeur d'un type inattendu. La trace S4T ne fait que protéger : elle n'autorise rien.
    """
    try:
        if type(target) is not int or not 0 < target < 10 ** 20:
            return 'unknown'
        if genealogy.get_family_relationship_bit(target) is not None:
            return 'family'
        info = sims.get(target)
        if info is None or info.sim_id != target:
            return 'unknown'
        household = info.household
        if household is None:
            return 'unknown'
        description, name = household.description, household.name
        player, played = household.is_player_household, household.is_played_household
    except Exception:
        return 'unknown'
    if type(description) is str and description.strip().upper().startswith(PREFIX[:3]):
        return 's4t'
    if type(name) is str and SUFFIX.strip().upper() in name.upper():
        return 's4t'
    if type(description) is not str or type(name) is not str or type(player) is not bool or type(played) is not bool:
        return 'unknown'
    return 'player' if player or played else 'npc'


def game_reset_npc_relations():
    """Premier bind seulement : efface les relations du foyer vers les PNJ identifiés. Rend le nombre effacé.

    Sont gardées : le foyer lui-même, la famille, tout foyer joueur, joué ou marqué S4T, et toute cible dont
    la provenance n'est pas établie (voir _relation_kind). destroy_relationship passe par RelationshipService :
    la paire est supprimée dans les deux sens, d'où la prudence. None : foyer actif ou gestionnaire des Sims
    indisponible, rien n'est effacé et la purge reste à faire.
    """
    import services
    household = services.active_household()
    if household is None:
        return None
    try:
        sims = services.sim_info_manager()
    except Exception:
        sims = None
    if sims is None:
        log('character', 'npc_scan:unavailable')
        return None
    infos = list(household.sim_info_gen())
    members = {info.sim_id for info in infos}
    removed = 0
    kept = {'family': 0, 'player': 0, 's4t': 0, 'unknown': 0}
    for info in infos:
        tracker = info.relationship_tracker
        genealogy = info.genealogy
        for target in list(tracker.target_sim_gen()):
            if type(target) is int and target in members:
                continue
            kind = _relation_kind(genealogy, target, sims)
            if kind != 'npc':
                kept[kind] += 1
                continue
            tracker.destroy_relationship(target, notify_client=True)
            removed += 1
    log('character', 'npc_scan:removed={},family={family},player={player},s4t={s4t},unknown={unknown}'.format(
        removed, **kept))
    return removed


def _clamp(funds):
    return max(0, min(MAX_FUNDS, funds)) if type(funds) is int else 0


def _character(value):
    """Personnage de l'ACK s'il porte les champs dont le mod a besoin, sinon None."""
    if not isinstance(value, dict) or not isinstance(value.get('characterId'), str) or not _UUID.match(value['characterId']):
        return None
    if value.get('status') not in ('pending_game', 'bound') or type(value.get('revision')) is not int:
        return None
    binding = value.get('binding')
    if value['status'] == 'bound' and not (isinstance(binding, dict) and isinstance(binding.get('gameHouseholdId'), str)):
        return None
    return value


class CharacterSync:
    """Machine à états appelée par AutoLink à chaque seconde, jamais d'exception vers le jeu."""

    def __init__(self, store, household=game_household, set_funds=game_set_funds,
                 reset_relations=game_reset_npc_relations, new_id=lambda: str(uuid.uuid4()), notify=game_notify,
                 clock=time.monotonic):
        self.store = store
        self.household = household
        self.set_funds = set_funds
        self.reset_relations = reset_relations
        self.new_id = new_id
        self.notify = notify
        self.clock = clock
        # Argent du foyer au dernier tick lié. Survit à reset() : il distingue un voyage (même argent en
        # mémoire du jeu) d'une partie rechargée, seul cas où le checkpoint serveur doit être restauré.
        self.last_seen = None
        self.recovery_required = False
        self.restore_failed = False
        # Informations déjà affichées dans cette session de jeu. Survit à reset() : un voyage ne les répète pas.
        self.informed = set()
        # Blocages par la garde solo depuis le lancement du jeu (diagnostic).
        self.solo_blocks = 0
        self.reset()

    def reset(self):
        """Reconnexion ou nouvelle zone : on redemande l'état au serveur."""
        self.state = 'idle'
        self.waiting = None
        self.waiting_body = None
        self.waiting_at = 0
        self.retry_ticks = 0
        self.character = None
        self.ticks = 0
        self.last_sent = None
        # Garde solo relue à la prochaine vérification : le launcher l'écrit avant de lancer le jeu.
        self.guard = _UNREAD
        self.solo_cause = None
        # Sauvegarde du jeu réussie pendant qu'une requête était en vol : checkpoint au tick suivant.
        self.pending_save = False

    def tick(self, manager, authenticated, loaded):
        if not authenticated:
            if self.character is not None and self.character.get('status') == 'bound' and not self.recovery_required:
                self.recovery_required = True
                try:
                    self.store.mark_character(self.character['characterId'] + '/recover', 'required')
                except (OSError, ValueError) as error:
                    log('character_recovery', type(error).__name__)
            if self.state != 'idle':
                self.reset()
            return None
        if not loaded:
            return None
        if self.waiting is not None:
            if self.waiting[1] == 'character.checkpoint' and self.waiting_body is not None and self.clock() - self.waiting_at >= 5:
                observed = self._observe()
                if (observed is not None and self.character is not None
                        and observed['householdId'] == self.character['binding']['gameHouseholdId']
                        and not self._solo_block(self.character, observed)):
                    # ACK perdu : meme UUID/corps, donc aucune double mutation cote serveur.
                    self.waiting_at = self.clock()
                    manager.request(self.waiting[1], dict(self.waiting_body), self.waiting[0])
                    return 'character.checkpoint'
            return None
        if self.retry_ticks > 0:
            self.retry_ticks -= 1
            return None
        if self.state == 'idle':
            return self._send(manager, 'character.resume', {}, 'resuming')
        if self.state == 'bound' and self.character is not None:
            observed = self._observe()
            # Avant tout checkpoint : un foyer solo ne part jamais vers le serveur.
            if self._solo_block(self.character, observed):
                return None
            if observed is not None and observed['householdId'] == self.character['binding']['gameHouseholdId']:
                self.last_seen = observed['funds']
                if self.clock() - getattr(self, '_save_context_at', -1000) >= 15:
                    from .save_context import publish
                    try:
                        if publish(manager, self.character, observed):
                            self._save_context_at = self.clock()
                    except (RuntimeError, ValueError, OSError):
                        pass  # Contexte local réessayé ; aucun impact sur checkpoint/jeu.
            self.ticks += 1
            if self.pending_save or self.ticks >= CHECKPOINT_TICKS:
                saved, self.pending_save, self.ticks = self.pending_save, False, 0
                return self._checkpoint(manager, observed, saved)
        return None

    def flush(self, manager, saved=False):
        """Sauvegarde du jeu ou déchargement de zone : checkpoint tout de suite, sans attendre CHECKPOINT_TICKS.

        `saved` : la partie vient d'être enregistrée ; le checkpoint part même sans changement, pour que le
        serveur date la dernière sauvegarde. Requête déjà en vol : envoi remis au tick suivant.
        """
        if self.state != 'bound' or self.character is None:
            return None
        if self.waiting is not None:
            self.pending_save = self.pending_save or saved
            return None
        observed = self._observe()
        if self._solo_block(self.character, observed):
            return None
        self.pending_save, self.ticks = False, 0
        return self._checkpoint(manager, observed, saved)

    def _observe(self):
        try:
            return self.household()
        except Exception as error:
            log('character_read', type(error).__name__)
            return None

    def _guard(self):
        if self.guard is _UNREAD:
            self.guard = solo_guard.load(self.store)
        return self.guard

    def _solo_block(self, character, observed):
        """Vrai si la garde solo interdit ce foyer à ce personnage : état `solo_blocked`, rien n'est appliqué."""
        cause = solo_guard.cause(self._guard(), character, observed)
        if cause is None:
            return False
        self.state = 'solo_blocked'
        self.solo_cause = cause
        self.solo_blocks += 1
        # Une ligne par transition, sans identifiant natif.
        log('character', 'solo_' + cause)
        if cause == 'binding_blocked':
            text = _SOLO_BINDING
        elif cause in ('guard_invalid', 'guard_other_character'):
            text = _SOLO_GUARD
        else:
            text = _SOLO_NEW if character.get('status') == 'pending_game' else _SOLO_OTHER
        self._inform('solo/' + cause + '/' + character['characterId'], text.format(character.get('name')))
        return True

    def solo_status(self):
        """Diagnostic en lecture seule : compteurs de la garde et cause du blocage, jamais d'identifiant natif."""
        value = solo_guard.summary(self._guard())
        value['cause'] = self.solo_cause
        value['blocks'] = self.solo_blocks
        return value

    def on_messages(self, manager, messages):
        for message in messages:
            if not isinstance(message, dict) or self.waiting is None or message.get('id') != self.waiting[0]:
                continue
            kind = self.waiting[1]
            self.waiting = None
            self.waiting_body = None
            if message.get('ok') is not True:
                self._refused(kind, message.get('code'))
                continue
            payload = message.get('payload') if isinstance(message.get('payload'), dict) else {}
            if kind == 'character.resume':
                self._resumed(manager, payload)
            elif kind == 'character.bind':
                self._bound(payload)
            elif kind == 'character.checkpoint':
                character = _character(payload.get('character'))
                if character is not None:
                    self.character = character
                    log('character', 'checkpoint')

    def _inform(self, key, text):
        """Information non critique : une fois par session de jeu, pas à chaque chargement de zone."""
        if key in self.informed:
            return
        self.informed.add(key)
        self.notify(TITLE, text)

    def _send(self, manager, kind, payload, state):
        request_id = self.new_id()
        manager.request(kind, payload, request_id)
        self.waiting = (request_id, kind)
        self.waiting_body = dict(payload)
        self.waiting_at = self.clock()
        self.state = state
        return kind

    def _refused(self, kind, code):
        code = code if isinstance(code, str) else 'error'
        log('character', kind + ':' + code)
        if kind == 'character.checkpoint' and code in ('storage_unavailable', 'request_limit', 'error'):
            # Refus avant confirmation durable : garder la partie courante et reprendre plus tard.
            self.state = 'bound'
            self.last_sent = None
            self.pending_save = True
            self.retry_ticks = 10
            return
        if kind == 'character.bind' and self.character is not None and code not in _TRANSIENT:
            # Refus définitif, même rejoué : ne plus le renvoyer à chaque reprise.
            self.store.mark_character(self.character['characterId'] + '/bind', 'done')
        # Révision dépassée : on relit l'état serveur ; sinon on attend la prochaine reconnexion.
        self.state = 'idle' if code == 'revision_conflict' else 'failed'
        if self.state == 'failed':
            self.notify(TITLE, 'Le serveur a refusé ce personnage ({}). Ouvre le launcher pour en choisir un autre.'.format(code))

    def _resumed(self, manager, payload):
        character = _character(payload.get('character'))
        if character is None:
            self.state = 'none'
            log('character', 'none_selected')
            self._inform('none', 'Aucun personnage choisi : sélectionne-le dans le launcher, page Personnages.')
            return
        self.character = character
        self.informed.discard('none')
        observed = self._observe()
        # Avant bind, rejeu et restauration. Une ancienne liaison vers un foyer solo n'est ni réparée ni rejouée.
        if self._solo_block(character, observed):
            return
        if observed is None:
            self.state = 'idle'
            return
        if character['status'] == 'pending_game':
            body = {'characterId': character['characterId'], 'gameHouseholdId': observed['householdId'],
                    'simIds': observed['simIds'], 'funds': _clamp(observed['funds'])}
            if observed.get('gameWorld'):
                body['gameWorld'] = observed['gameWorld']
            if observed.get('homeLotId') and observed['homeLotId'] == character.get('lotId'):
                body['lotId'] = observed['homeLotId']
            self._send_bind(manager, body)
            return
        if character['binding']['gameHouseholdId'] != observed['householdId']:
            self.state = 'mismatch'
            log('character', 'household_mismatch')
            self.notify(TITLE, "Ce foyer n'est pas celui de {}. Charge son foyer ou choisis un autre personnage.".format(character.get('name')))
            return
        pending = self._pending_bind(character['characterId'])
        if pending is not None and pending['body'].get('gameHouseholdId') != observed['householdId']:
            # Marqueur d'un autre foyer : gardé tel quel, rejeu remis à une reprise où il correspondra.
            log('character', 'bind_marker_incompatible')
            pending = None
        if pending is not None:
            # ACK du bind perdu ou action du jeu en échec : même UUID et même contenu, le serveur renvoie
            # l'ACK initial (applyFunds, resetNpcRelations) ; les marqueurs empêchent une double application.
            manager.request('character.bind', pending['body'], pending['id'])
            self.waiting = (pending['id'], 'character.bind')
            self.state = 'binding'
            return
        if self.store.character_marker(character['characterId'] + '/recover') == 'required':
            self.recovery_required = True
        checkpoint = payload.get('checkpoint')
        if self.recovery_required and checkpoint is None and type(character.get('money')) is int:
            # Première coupure avant le premier checkpoint : le bind confirmé porte déjà les fonds serveur.
            checkpoint = {'funds': character['money'], 'lastGameWorld': character.get('gameWorld')}
        restored = self._restore(checkpoint, observed)
        if self.restore_failed:
            self.state = 'recovery_blocked'
            self._inform('recovery/' + character['characterId'],
                         'Reprise sécurisée en attente : le dernier état serveur ne peut pas être restauré. Aucun progrès local envoyé.')
            return
        if self.recovery_required:
            try:
                self.store.mark_character(character['characterId'] + '/recover', 'done')
            except (OSError, ValueError) as error:
                log('character_recovery', type(error).__name__)
                self.state = 'recovery_blocked'
                return
        self.recovery_required = False
        self.state = 'bound'
        if restored is not None:
            # Le jeu a été modifié : toujours signalé.
            self.notify(TITLE, 'Bon retour, {} ! Argent restauré : {} §.'.format(character.get('name'), restored))
        else:
            self._inform('resumed/' + character['characterId'], 'Bon retour, {} ! Partie reprise.'.format(character.get('name')))

    def _send_bind(self, manager, body):
        request_id = self.new_id()
        # Enregistré avant l'envoi : une déconnexion avant l'ACK le fera rejouer à l'identique.
        self.store.mark_character(body['characterId'] + '/bind', json.dumps({'id': request_id, 'body': body}))
        manager.request('character.bind', body, request_id)
        self.waiting = (request_id, 'character.bind')
        self.state = 'binding'

    def _pending_bind(self, character_id):
        value = self.store.character_marker(character_id + '/bind')
        if value is None or value == 'done':
            return None
        try:
            pending = json.loads(value)
        except ValueError:
            return None
        if not isinstance(pending, dict) or not isinstance(pending.get('id'), str) or not _UUID.match(pending['id']) \
                or not isinstance(pending.get('body'), dict) or pending['body'].get('characterId') != character_id:
            return None
        return pending

    def _restore(self, checkpoint, observed):
        # Reprise : les fonds du checkpoint serveur font foi après un rechargement de partie (solo, autre PC…).
        # Un voyage garde l'argent du jeu, plus récent que le checkpoint : le prochain checkpoint l'enverra.
        self.restore_failed = False
        if (not isinstance(checkpoint, dict) or type(checkpoint.get('funds')) is not int
                or not 0 <= checkpoint['funds'] <= MAX_FUNDS):
            self.restore_failed = self.recovery_required
            return None
        self.last_sent = (checkpoint['funds'], checkpoint.get('lastGameWorld'))
        if checkpoint['funds'] == observed['funds'] or (not self.recovery_required and observed['funds'] == self.last_seen):
            return None
        if not self._apply(self.set_funds, checkpoint['funds']):
            self.restore_failed = True
            return None
        log('character', 'funds_restored')
        return checkpoint['funds']

    @staticmethod
    def _apply(action, *args):
        """Action native : vrai seulement si elle a abouti (None ou False = foyer absent)."""
        try:
            result = action(*args)
        except Exception as error:
            log('character_action', type(error).__name__)
            return False
        return result is not None and result is not False

    def _bound(self, payload):
        character = _character(payload.get('character'))
        if character is None:
            self.state = 'failed'
            log('character', 'bind_invalid_ack')
            return
        current = self.character
        if current is None or current.get('characterId') != character['characterId'] or \
                current['revision'] <= character['revision']:
            self.character = character  # Un ACK rejoué porte la révision du bind : garder la plus récente.
        # ACK arrivé après coup : le foyer est relu avant toute action native, aucun marqueur n'est posé sinon.
        observed = self._observe()
        if self._solo_block(character, observed):
            return
        if observed is None or observed['householdId'] != character['binding']['gameHouseholdId']:
            self.state = 'idle'
            log('character', 'bind_ack_stale')
            return
        key = character['characterId']
        # Marqueurs posés après succès seulement. Les deux actions sont idempotentes (montant absolu, purge) :
        # un nouvel essai à la reprise suivante ne double rien ; tant qu'il en reste, le bind reste rejouable.
        complete = True
        if type(payload.get('applyFunds')) is int and self.store.character_marker(key + '/funds') is None:
            if self._apply(self.set_funds, payload['applyFunds']):
                self.store.mark_character(key + '/funds', 'done')
                log('character', 'funds_applied')
            else:
                complete = False
                log('character', 'funds_failed')
        if payload.get('resetNpcRelations') is True and self.store.character_marker(key + '/npc') is None:
            try:
                removed = self.reset_relations()
            except Exception as error:
                removed = None
                log('character_action', type(error).__name__)
            if removed is None:
                complete = False
                log('character', 'npc_failed')
            else:
                self.store.mark_character(key + '/npc', 'done')
                log('character', 'npc_reset_' + str(removed))
        if complete:
            self.store.mark_character(key + '/bind', 'done')
        self.state = 'bound'
        log('character', 'bound')
        self.notify(TITLE, '{} est relié à ce foyer et sauvegardé sur le serveur.'.format(character.get('name')))

    def _checkpoint(self, manager, observed, force=False):
        if observed is None or self.character is None:
            return None
        if observed['householdId'] != self.character['binding']['gameHouseholdId']:
            return None
        current = (_clamp(observed['funds']), observed.get('gameWorld'))
        if current == self.last_sent and not force:
            return None
        body = {'characterId': self.character['characterId'], 'expectedRevision': self.character['revision'],
                'funds': current[0], 'lastGameWorld': current[1]}
        lot = self.character.get('lotId')
        if isinstance(lot, str) and _NATIVE.match(lot) and lot == observed.get('homeLotId'):
            body['lastLotId'] = lot
        self.last_sent = current
        return self._send(manager, 'character.checkpoint', body, 'bound')
