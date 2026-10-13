"""Garde des parties solo : foyers et Sims que le personnage serveur choisi ne doit jamais toucher.

Le launcher écrit dans config.json, avant de lancer le jeu :
`solo_guard {characterId: uuid, blockedHouseholdIds: [décimal], blockedSimIds: [décimal]}`.
Le mod ne fait que lire. Clé absente : pas de garde (ancien launcher, CLI, tests). Clé présente mais
illisible ou mal formée : garde fermée, le mod ne lie, ne restaure et ne sauvegarde rien.

Reprise : un personnage que le serveur authentifié renvoie `bound` n'est pas bloqué quand le foyer observé est
EXACTEMENT sa liaison (même gameHouseholdId, même ensemble de simIds). Cela couvre une carte multijoueur déjà liée
que le relevé du launcher aurait rangée parmi les parties solo. Aucune exception pour une première liaison
(`pending_game`), un foyer partiel, un autre foyer, une liaison mal formée, ni pour une garde invalide ou
écrite pour un autre personnage.

Sauvegarde choisie : `allowedSaveSlotId` (facultatif, décimal) est le slot_id d'une sauvegarde que le joueur a
explicitement choisie dans le launcher pour ce personnage. Seule cette sauvegarde échappe aux listes ; une copie
« Enregistrer sous » a un autre slot_id et reste protégée. slot_id illisible dans le jeu : rien n'est autorisé.
"""
import re

MAX_IDS = 2048
_UUID = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')
_NATIVE = re.compile(r'^[1-9][0-9]{0,19}$')
# Garde présente mais inutilisable : tout est bloqué.
INVALID = 'invalid'


def _ids(value):
    if not isinstance(value, list) or len(value) > MAX_IDS:
        return None
    if any(not isinstance(item, str) or not _NATIVE.match(item) for item in value):
        return None
    return frozenset(value)


def parse(settings):
    """None si la clé est absente, INVALID si elle est mal formée, sinon la garde validée."""
    if not isinstance(settings, dict) or 'solo_guard' not in settings:
        return None
    value = settings['solo_guard']
    if not isinstance(value, dict):
        return INVALID
    character_id = value.get('characterId')
    households = _ids(value.get('blockedHouseholdIds'))
    sims = _ids(value.get('blockedSimIds'))
    allowed = value.get('allowedSaveSlotId')
    if not isinstance(character_id, str) or not _UUID.match(character_id) or households is None or sims is None:
        return INVALID
    if allowed is not None and (not isinstance(allowed, str) or not _NATIVE.match(allowed)):
        return INVALID
    return {'characterId': character_id, 'households': households, 'sims': sims, 'allowedSave': allowed}


def load(store):
    """Garde lue dans la configuration du mod ; configuration illisible : garde fermée."""
    try:
        return parse(store.settings())
    except (ValueError, OSError):
        return INVALID


def cause(guard, character, observed):
    """Raison du blocage pour ce personnage et ce foyer observé, ou None si rien n'est bloqué."""
    if guard is None:
        return None
    if guard == INVALID:
        return 'guard_invalid'
    if guard['characterId'] != character.get('characterId'):
        return 'guard_other_character'
    if exact_resume(character, observed) or chosen_save(guard, observed):
        return None
    binding = character.get('binding')
    if isinstance(binding, dict) and _hit(guard, binding.get('gameHouseholdId'), binding.get('simIds')):
        return 'binding_blocked'
    if observed is None:
        return None
    if observed.get('householdId') in guard['households']:
        return 'household_blocked'
    if _hit(guard, None, observed.get('simIds')):
        return 'sim_blocked'
    return None


def _sim_set(value):
    """Ensemble des identifiants natifs d'une liste bien formée, sans doublon et non vide ; sinon None."""
    if not isinstance(value, list) or not 0 < len(value) <= MAX_IDS:
        return None
    if any(not isinstance(item, str) or not _NATIVE.match(item) for item in value):
        return None
    ids = frozenset(value)
    return ids if len(ids) == len(value) else None


def exact_resume(character, observed):
    """Vrai si le foyer observé est exactement la liaison d'un personnage `bound` (identifiants, jamais les noms)."""
    if not isinstance(character, dict) or character.get('status') != 'bound' or not isinstance(observed, dict):
        return False
    binding = character.get('binding')
    if not isinstance(binding, dict):
        return False
    household = binding.get('gameHouseholdId')
    if not isinstance(household, str) or not _NATIVE.match(household) or observed.get('householdId') != household:
        return False
    bound, seen = _sim_set(binding.get('simIds')), _sim_set(observed.get('simIds'))
    if bound is None or seen is None or not bound.issubset(seen):
        return False
    if bound == seen:
        return True
    # Une adoption de chat/chien/cheval ne change pas l'identité des joueurs.
    # Seuls les nouveaux membres explicitement identifiés animaux par le jeu sont tolérés.
    pets = _sim_set(observed.get('petSimIds'))
    return pets is not None and pets.issubset(seen) and (seen - bound).issubset(pets)


def chosen_save(guard, observed):
    """Vrai si la partie chargée est la sauvegarde choisie par le joueur pour ce personnage."""
    allowed = guard.get('allowedSave') if isinstance(guard, dict) else None
    return allowed is not None and isinstance(observed, dict) and observed.get('saveSlotId') == allowed


def _hit(guard, household_id, sim_ids):
    if isinstance(household_id, str) and household_id in guard['households']:
        return True
    return isinstance(sim_ids, list) and any(isinstance(s, str) and s in guard['sims'] for s in sim_ids)


def summary(guard):
    """Compteurs pour le diagnostic : jamais d'identifiant natif."""
    if guard is None:
        return {'guard': 'absent', 'blockedHouseholds': 0, 'blockedSims': 0, 'chosenSave': False}
    if guard == INVALID:
        return {'guard': 'invalid', 'blockedHouseholds': 0, 'blockedSims': 0, 'chosenSave': False}
    return {'guard': 'active', 'blockedHouseholds': len(guard['households']), 'blockedSims': len(guard['sims']),
            'chosenSave': guard['allowedSave'] is not None}
