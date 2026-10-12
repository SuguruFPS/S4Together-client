"""Contrat d'apparence d'un Sim distant (spike rendu distant, 12/10/2026). Python pur, aucun import du jeu.

Le profil est une sérialisation `EA.Sims4.Persistence.SimData` réduite aux seuls champs d'apparence. Numéros et
types de champs relevés dans `protocolbuffers/FileSerialization_pb2.pyc` (Game/Bin/Python/generated.zip) du jeu
1.128.90.1030. Tout autre champ (identité, foyer, argent, besoins, humeur, inventaire, relations, position) est
refusé à la réception et retiré à l'export : un profil ne peut donc rien écrire d'autre qu'une apparence.
Rien ici ne crée de Sim : NOT_TESTED_GAME.
"""
import base64
import binascii
import hashlib
import re

FORMAT = 'simdata-allowlist-v1'
VERSION = 1
GAME_VERSION = '1.128.90.1030'
MAX_BYTES = 262144
VARINT, FIXED64, BYTES, FIXED32 = 0, 1, 2, 5

# Numéro de champ SimData -> (nom, type de fil attendu).
APPEARANCE_FIELDS = {
    7: ('gender', VARINT), 8: ('age', VARINT), 9: ('voice_pitch', FIXED32), 10: ('skin_tone', VARINT),
    11: ('voice_actor', VARINT), 12: ('physique', BYTES), 18: ('facial_attr', BYTES), 21: ('outfits', BYTES),
    28: ('genetic_data', BYTES), 38: ('current_outfit_type', VARINT), 39: ('current_outfit_index', VARINT),
    54: ('voice_effect', VARINT), 60: ('extended_species', VARINT), 63: ('pelt_layers', BYTES),
    67: ('skin_tone_val_shift', FIXED32),
}
# Sans eux, le jeu n'a pas de quoi construire un corps habillé : profil refusé plutôt que complété au hasard.
REQUIRED_FIELDS = (7, 8, 10, 12, 18, 21)

_GAME = re.compile(r'^[0-9]{1,4}(\.[0-9]{1,6}){3}$')
_SHA = re.compile(r'^[0-9a-f]{64}$')
_BASE64 = re.compile(r'^[A-Za-z0-9+/]*={0,2}$')


def _varint(data, index):
    value = shift = 0
    while True:
        if index >= len(data) or shift > 63:
            raise ValueError('varint')
        byte = data[index]
        index += 1
        value |= (byte & 0x7F) << shift
        shift += 7
        if byte < 0x80:
            return value, index


def scan(data):
    """Champs de premier niveau d'un message protobuf : [(numéro, type de fil, début, fin)]. ValueError si mal formé."""
    result, index = [], 0
    while index < len(data):
        start = index
        key, index = _varint(data, index)
        number, wire = key >> 3, key & 7
        if number == 0:
            raise ValueError('field')
        if wire == VARINT:
            _, index = _varint(data, index)
        elif wire == BYTES:
            length, index = _varint(data, index)
            index += length
        elif wire in (FIXED64, FIXED32):
            index += 8 if wire == FIXED64 else 4
        else:
            raise ValueError('wire')
        if index > len(data):
            raise ValueError('truncated')
        result.append((number, wire, start, index))
    return result


def filter_simdata(data):
    """Export : ne garde d'un SimData sérialisé que les champs d'apparence, une occurrence chacun. ValueError sinon."""
    kept, seen = [], set()
    for number, wire, start, end in scan(data):
        expected = APPEARANCE_FIELDS.get(number)
        if expected is None:
            continue
        if wire != expected[1] or number in seen:
            raise ValueError('appearance_field')
        seen.add(number)
        kept.append(data[start:end])
    return b''.join(kept)


def check_appearance(data):
    """None si `data` ne contient que des champs d'apparence valides et tous les champs requis, sinon un code."""
    if not isinstance(data, bytes) or not 0 < len(data) <= MAX_BYTES:
        return 'profile_size'
    try:
        fields = scan(data)
    except ValueError:
        return 'profile_malformed'
    seen = set()
    for number, wire, _, _ in fields:
        expected = APPEARANCE_FIELDS.get(number)
        if expected is None:
            return 'profile_forbidden_field'
        if wire != expected[1] or number in seen:
            return 'profile_malformed'
        seen.add(number)
    if any(number not in seen for number in REQUIRED_FIELDS):
        return 'profile_incomplete'
    return None


def build_profile(data, game_version=GAME_VERSION):
    """Enveloppe transportable d'une apparence déjà filtrée. ValueError si elle ne respecte pas le contrat."""
    code = check_appearance(data)
    if code is not None:
        raise ValueError(code)
    return {'version': VERSION, 'format': FORMAT, 'game': game_version,
            'sha256': hashlib.sha256(data).hexdigest(), 'data': base64.b64encode(data).decode('ascii')}


def validate_profile(profile, game_version=GAME_VERSION):
    """(octets, sha256, None) pour une enveloppe valide, sinon (None, None, code). Aucun champ supplémentaire."""
    if not isinstance(profile, dict) or set(profile) != {'version', 'format', 'game', 'sha256', 'data'}:
        return None, None, 'profile_shape'
    if type(profile['version']) is not int or profile['version'] != VERSION or profile['format'] != FORMAT:
        return None, None, 'profile_version'
    game, sha, text = profile['game'], profile['sha256'], profile['data']
    if not isinstance(game, str) or not _GAME.match(game):
        return None, None, 'profile_shape'
    if game != game_version:
        # Les numéros de champs sont ceux d'une version précise du jeu : pas d'import entre versions.
        return None, None, 'game_version_mismatch'
    if not isinstance(sha, str) or not _SHA.match(sha) or not isinstance(text, str):
        return None, None, 'profile_shape'
    if len(text) > (MAX_BYTES * 4) // 3 + 4 or len(text) % 4 or not _BASE64.match(text):
        return None, None, 'profile_size' if len(text) > (MAX_BYTES * 4) // 3 + 4 else 'profile_malformed'
    try:
        data = base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError):
        return None, None, 'profile_malformed'
    if hashlib.sha256(data).hexdigest() != sha:
        return None, None, 'profile_hash'
    code = check_appearance(data)
    if code is not None:
        return None, None, code
    return data, sha, None
