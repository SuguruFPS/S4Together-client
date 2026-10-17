"""Contexte de partie chargée vers le pont LOCAL ; aucun fichier ou fonds envoyés."""
import re

def publish(manager, character, observed):
    if not getattr(manager, 'launcher_bridge', False) or not getattr(manager, 'authenticated', False):
        return False
    if not isinstance(character, dict) or character.get('status') != 'bound' or not isinstance(observed, dict):
        return False
    binding = character.get('binding', {})
    slot = observed.get('saveSlotId')
    if (not isinstance(slot, str) or not re.match(r'^[1-9][0-9]{0,19}$', slot)
            or observed.get('householdId') != binding.get('gameHouseholdId')
            or not binding.get('simIds') or not isinstance(observed.get('simIds'), list)
            or not set(binding['simIds']).issubset(observed['simIds'])):
        return False
    manager.request('local.save-context', {'characterId': character['characterId'],
                    'householdId': observed['householdId'], 'simIds': observed['simIds'], 'slotId': slot})
    return True
