# Extensions abstraites Alpha 1

## Personnages (T3)

Contrat serveur final, enveloppe v1 inchangée `{v:1,id:"UUID",type,payload}`. Acteur et monde exclusivement issus de la session authentifiée ; aucun `playerId`, `worldId` ni `sessionId` dans les payloads. ACK privés v1 ; aucune donnée personnage/binding/checkpoint dans les snapshots publics. Import fixé : export manuel vers la Bibliothèque native en solo, placement dans la partie S4Together, observation normalisée par le mod. S4Together ne lit ni ne modifie une sauvegarde solo ; aucun fichier, blob, relation PNJ ou chemin reçu.

| Message | Payload exact | ACK `payload` exact |
|---|---|---|
| `character.list` | `{}` | `{characters:[Character],selectedCharacterId:UUID\|null,startingMoney:integer,maxCharacters:integer}` |
| `character.create` | `{name:string,origin:"cas"\|"import",keepMoney:boolean,keepHouse:boolean,lotId?:string\|null}` | `{character:Character,selectedCharacterId:UUID}` |
| `character.select` | `{characterId:UUID}` | `{character:Character,selectedCharacterId:UUID}` |
| `character.bind` | `{characterId:UUID,gameHouseholdId:string,simIds:[string],funds:integer,gameWorld?:string\|null,lotId?:string\|null}` | `{character:Character,applyFunds:integer\|null,resetNpcRelations:boolean}` |
| `character.checkpoint` | `{characterId:UUID,expectedRevision:integer,funds:integer,lastLotId?:string\|null,lastGameWorld?:string\|null}` | `{character:Character,checkpoint:Checkpoint}` |
| `character.resume` | `{}` | `{character:Character\|null,checkpoint:Checkpoint\|null}` |
| `lot.list` | `{}` | `{lots:[{id:string,name:string,ownerId:UUID\|null,householdId:UUID\|null,requiredDlcs:[string]}]}` |

`Character` possède exactement `{characterId:UUID,name:string,origin:"cas"|"import",householdId:UUID,lotId:string|null,gameWorld:string|null,money:integer|null,moneyPolicy:"server_start"|"imported",houseImported:boolean,npcRelations:"reset",status:"pending_game"|"bound",binding:Binding|null,checkpoint:Checkpoint|null,revision:integer}`.
`Binding` possède exactement `{gameHouseholdId:string,simIds:[string]}`. `Checkpoint` possède exactement `{revision:integer,funds:integer,lastLotId:string|null,lastGameWorld:string|null,savedAt:string}` ; `savedAt` est une date UTC ISO8601 horodatée côté serveur. Avant tout checkpoint, `checkpoint:null` (aucun état final inventé).

Validation stricte : champs inconnus/doublons refusés, types exacts, UUID au format canonique `xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx` (minuscules), nom de 1–40 caractères avant trim, non vide après trim, aucun contrôle. Identifiants natifs : uniquement chiffres ASCII, 1–20 caractères, `simIds` de 1–8 identifiants distincts. `gameWorld`/`lastGameWorld` : texte non vide de 1–64 caractères sans contrôle, ou null/absent. `lotId`/`lastLotId` : identifiant du catalogue existant (maximum 100 caractères) ou identifiant natif numérique 1–20 ; null/absent permis sauf maison importée. Fonds : entier JSON 0–9999999, puis plafonnement à `Server:MaxImportedMoney` (défaut 9999999) pour les observations bind/import/checkpoint ; ni fractions ni valeurs hors bornes. `expectedRevision` : entier >=1, égal à la révision courante du personnage.

`Server:StartingMoney` défaut 20000 (0–9999999), `Server:MaxCharacters` défaut 4 (1–4), `Server:MaxImportedMoney` défaut 9999999 (0–9999999). Création : UUID personnage + foyer `Household` existant via `HouseholdService` + éventuel claim exclusif via `PropertyService` + sélection durable dans une seule transaction. `keepMoney:false` donne `moneyPolicy:"server_start"`, `money:StartingMoney` capturé à la création ; `keepMoney:true` donne `moneyPolicy:"imported"`, `money:null` jusqu'au premier bind, quelle que soit l'origine. `keepHouse:true` exige `origin:"import"` et `lotId` non null ; sinon `invalid_request`. `houseImported` décrit ce choix, sans preuve de placement natif. Les relations PNJ sont toujours `"reset"` ; aucun graphe solo stocké. Quota explicite par joueur, plafond dur 4. La section JSON UTF-8 `{characters,characterSelections}` du monde est limitée à 65536 octets, refus `character_limit` avant commit.

Premier bind : statut `bound`, binding persisté, `gameWorld` observé (ou null), fonds importés fixés si encore null. `applyFunds` = fonds initiaux capturés uniquement pour `server_start` au premier bind, sinon null ; `resetNpcRelations:true` uniquement au premier bind. Même `gameHouseholdId` déjà lié : ACK de l'état courant sans modification, `applyFunds:null`, `resetNpcRelations:false` (les autres observations valides ne remplacent pas le binding). Autre identifiant : `character_already_bound`. Un foyer natif ou Sim déjà lié à un autre personnage du monde : `character_binding_conflict`. Le binding et son premier ACK ne constituent pas une preuve d'application en jeu.

Révision du personnage : 1 à la création, +1 au premier bind, +1 à chaque checkpoint ; sélection/rebind sans incrément. Checkpoint réservé à un personnage bound, met à jour `money`, `gameWorld` et le checkpoint. Les localisations optionnelles absentes/null effacent ces localisations dans le nouveau checkpoint ; elles ne transfèrent pas la résidence. `Character.lotId` est toujours la résidence actuelle dérivée des claims (y compris après `property.release/claim`). `character.select` persiste entre reconnect/restart ; création sélectionne le nouveau personnage. Reprise sans sélection : `{character:null,checkpoint:null}`. ACL propriétaire pour toute mutation ; personnage inconnu ou tiers : `forbidden`, sans fuite d'existence.

Lots : `lot.list` expose le catalogue du monde, avec propriétaire actuel même hors ligne ; le catalogue initial contient des lots abstraits de test, sans catalogue natif EA. Pour permettre le placement natif T3, un nouveau `lotId` numérique peut être déclaré lors de `character.create` ou `property.claim` ; il est créé libre puis claimé atomiquement et exclusivement, maximum 1024 lots par monde (`lot_limit`). Un identifiant non numérique absent est refusé `not_found`. Un seul lot/résidence par foyer ; aucun abandon à la déconnexion. DLC contrôlés pour les lots déjà connus. Les localisations bind/checkpoint doivent être des lots déjà déclarés et accessibles (DLC) ; aucune déclaration/appropriation implicite lors de ces observations.
Maisons natives (mod, 12/10/2026) : le `lotId` numérique publié par le mod est le `lot_description_id` du terrain (identique dans toutes les sauvegardes du même monde), jamais `zone_id`/`lot_id` (propres à chaque sauvegarde). Le mod (`s4t/home_lots.py`) réserve la maison habitée par le foyer lié (`property.release` de l'ancienne puis `property.claim`), ne réessaie pas un `lot_taken`, refuse les appartements et retrouve dans la partie locale le terrain de chaque autre joueur pour l'annoncer « Nom (S4T) ». Aucun foyer distant n'est créé dans la sauvegarde : NOT_TESTED_GAME.

Mutations et erreurs de domaine dédupliquées par UUID + contenu exact comme v1, sous verrou, clone, validation, journal durable et `store.Save` (JSON atomique / SuguruDB `If-Match` CAS) avant ACK. Même UUID/contenu : ACK/erreur initial exact, y compris `applyFunds/resetNpcRelations` du premier bind ; le client déduplique donc aussi l'application native par UUID. Autre contenu : `request_id_conflict`. Lectures list/resume/lot.list sans cache ni écriture. Un refus ne laisse ni foyer ni claim ni personnage partiel. Migration additive schéma 1 : `characters:{}`, `characterSelections:{}` par défaut, mêmes stores et transaction, aucun document séparé.

Codes d'erreur : `invalid_request`, `unauthorized`, `forbidden`, `character_limit` (quota ou 64 Kio), `character_already_bound`, `character_binding_conflict`, `character_not_bound`, `revision_conflict`, `lot_limit`, `not_found`, `lot_taken`, `residence_exists`, `dlc_missing`, `household_limit`, `request_id_conflict`, `request_limit`, `storage_unavailable` ; aucun changement des codes des anciennes commandes. **Serveur seulement : NOT_TESTED_GAME**, fonds observés déclaratifs et maison/purge PNJ/application native à vérifier côté mod ; aucune économie anti-triche native revendiquée.

## Saison initiale commune (carte #138, extension saison v1)

Enveloppe réseau inchangée `{v:1,id:"UUID",type,payload}`. `hello` accepté et `snapshot` contiennent `snapshot.season` :

| État | Objet `season` |
|---|---|
| Jeu de base / aucun candidat | `{v:1,status:"none"}` |
| Réservation | `{v:1,status:"pending",owner:"player UUID",expiresAt:"UTC ISO 8601"}` |
| Choix durable | `{v:1,status:"chosen",season:"spring\|summer\|autumn\|winter",chosenBy:"player UUID",chosenAt:"UTC ISO 8601"}` |

Le premier **HELLO accepté éligible**, dans l'ordre d'admission serveur, reçoit le bail. Une requête `/health`, un snapshot sans joueur, une authentification refusée ou un HELLO rejeté ne réservent rien. L'éligibilité est `DLCService.CanChooseSeason` : manifeste normalisé contenant **EP05** et configuration `Server:AllowSeasons=true` (défaut). `Server:SeasonLeaseSeconds` vaut 120 par défaut, borné de 1 à 3600. Aucun candidat : `none`, sans bloquer l'entrée. Un client sans EP05 peut recevoir les métadonnées communes, mais ne doit jamais les appliquer aux APIs DLC du jeu ou proposer le choix. La déclaration DLC n'est pas une preuve de licence : sa détection fiable reste à la charge du client et de la validation en jeu.

| Commande | Payload exact | ACK `payload` |
|---|---|---|
| `season.choose` | `{season:"spring\|summer\|autumn\|winter"}` | `{season:<état v1>,sequence}` |
| `season.abandon` | `{}` | `{season:<état v1>,sequence}` |
| `admin.season.reset` | `{reason:"motif non vide, 500 caractères maximum"}` | `{season:<état v1>,sequence}` |

L'acteur et sa session proviennent exclusivement de la connexion authentifiée. Valeurs sensibles à la casse, aucun champ supplémentaire ni doublon. Erreurs : `unauthorized`, `invalid_request`, `invalid_season`, `dlc_missing`, `season_not_owner`, `role_forbidden`, `request_id_conflict`, `request_limit`, `storage_unavailable`. Tant que le bail est actif, seul son détenteur choisit. Une fois le choix validé, les autres joueurs éligibles reçoivent un ACK contenant **la saison commune validée**, même s'ils demandaient une autre saison ; aucun remplacement. Une valeur invalide ou un joueur inéligible reste refusé. Le rejeu UUID/payload identique retourne l'ACK initial persisté ; celui-ci peut être historique après un reset explicite : relire un snapshot pour l'état actuel. Le rôle owner courant est vérifié avant le cache d'un reset.

Diffusions `{v:1,type:"season.chosen",payload:{worldId,sequence,season}}` après le premier choix durable, et `season.state` de même forme après les transitions de bail/reset. Les clients utilisent `sequence` pour rejeter les événements périmés et demandent un snapshot en cas de divergence. Le bail est UTC, attaché à une session interne non exposée ; expiration vérifiée sous verrou et par timer serveur chaque seconde, déconnexion/abandon/kick/ban transférant au prochain candidat. Une session ayant abandonné ou expiré attend une reconnexion ou un reset ; elle ne monopolise pas une nouvelle réservation. Le timer ne fait aucune mutation mémoire/diffusion si la sauvegarde échoue. La déconnexion réseau reste effective si le stockage est indisponible ; le bail durable est réconcilié dès la reprise du stockage.

Migration **additive du schéma monde 1** : anciens documents sans `initialSeason`/`seasonResetEvents` reçoivent `none`/`[]`. Ces champs partagent le clone, verrou, validation et transaction du monde (remplacement durable JSON avec `.bak`, document SuguruDB CAS `If-Match`), avec le cache des requêtes ; aucun fichier ou document saison distinct. Le choix persiste au restart. Une réservation ancienne ne survit pas comme droit actif sans sa session : le prochain HELLO accepté ou la prochaine réconciliation libère/transfère ce bail.

Reset exclusivement `owner` : motif obligatoire, audit privé `seasonResetEvents` `{actorId,reason,previous,at}` (UTC, 1000 entrées maximum), copie complète de l'ancien état saison dans la même écriture, incrément de séquence et nouvelle réservation parmi les joueurs éligibles présents. Aucun restart ne reset une saison choisie. La commande authentifiée constitue l'action explicite ; le frontend doit afficher la confirmation avant son envoi. `admin.permissions.get` annonce `admin.season.reset` uniquement aux owners. Aucun journal d'audit diffusé publiquement.

**Limite : serveur et protocole uniquement.** Le client Python existant conserve ce champ additif dans HELLO/snapshot, testé sans modification de ses hooks. Launcher, UI et application native de la saison, progression de calendrier/saison/météo, compatibilité de packs et effet en jeu ne sont pas implémentés ici : `NOT_TESTED_GAME`.

## Administration des rôles (carte #126)

Extension additive de l'enveloppe **v1** : `{v:1,id:"UUID",type,payload}` ; ACK/erreurs v1 corrélés. Un rôle unique par identité dans ce stockage serveur : `owner > admin > moderator > player`. Sans entrée persistée, le rôle est `player`, y compris pour les anciens mondes. Aucun rôle de `hello` ou d'un payload client n'est une autorité.

| Type | Payload exact | Payload de l'ACK |
|---|---|---|
| `admin.role.assign` | `{targetPlayerId:"UUID",role:"owner\|admin\|moderator\|player"}` | `{targetPlayerId,oldRole,newRole,sequence}` |
| `admin.role.revoke` | `{targetPlayerId:"UUID"}` | `{targetPlayerId,oldRole,newRole:"player",sequence}` |
| `admin.permissions.get` | `{}` | `{worldId,playerId,role,permissions,assignableRoles,sanctions,sanctionLimits,sequence}` |

L'acteur est exclusivement l'identité authentifiée de la connexion. Les champs supplémentaires, doublons de champs, types incorrects et rôles inconnus donnent `invalid_request`. L'attribution exige que **le rôle actuel de la cible et le nouveau rôle soient strictement inférieurs** à celui de l'acteur. Le retrait exige la même infériorité du rôle actuel ; il revient à `player`. Aucun acteur ne peut modifier son propre rôle ni celui d'un pair. Après validation du format, les droits et le rôle attribuable de l'acteur sont vérifiés avant toute existence de cible ou protection du dernier owner. Un acteur sans droit reçoit `role_forbidden`. Seul un owner autorisé à demander une rétrogradation reçoit `last_owner` pour le dernier owner ; les autres violations de hiérarchie donnent `role_forbidden`. Cible autorisée absente : `not_found` ; session absente : `unauthorized`. Aucun owner ne peut attribuer `owner` par WebSocket.

`permissions` contient `admin.permissions.get` pour tous ; `admin.role.assign`, `admin.role.revoke`, `admin.sanction.apply`, `admin.sanction.revoke` et `admin.sanction.list` pour moderator/admin/owner. `assignableRoles` énumère les rôles strictement inférieurs, sous réserve du rôle actuel de la cible. `sanctions` et `sanctionLimits` sont définis ci-dessous. Aucune capacité stop/restore ou exception aux ACL foyer/terrain n'est accordée. La lecture retourne les droits actuels, même si son UUID est réutilisé ; aucune écriture ni cache durable pour cette lecture.

Les mutations suivent le verrou, clone, déduplication persistante et écriture atomique du monde existants ; aucune nouvelle base. Réutiliser exactement un UUID/payload retourne le résultat initial sans réappliquer ni doubler l'audit (cela n'accorde aucun nouveau droit après révocation). UUID réutilisé avec un autre contenu : `request_id_conflict`. Une panne donne `storage_unavailable` sans rôle, séquence ni audit publié. Un changement effectif augmente `sequence` et ajoute au journal borné `roleEvents` `{actorId,targetId,oldRole,newRole,at}` (UTC, 1000 entrées les plus récentes), dans la même transaction que `playerRoles`. Les commandes sans changement effectif n'ajoutent pas d'audit. Ce journal est local au stockage ; aucun endpoint d'audit ni diffusion publique n'est ajouté.

Amorçage explicite hors protocole : console locale `owner PLAYERID`, seulement si aucun owner n'existe, pour une identité déjà enregistrée. Persistance atomique avec audit `actorId:"local-console"`. Refus `owner_exists`, `not_found` ou `storage_unavailable`. Ni premier arrivant promu automatiquement, ni commande réseau d'amorçage. Plusieurs owners préexistants dans un document valide restent protégés par la hiérarchie. La liaison Discord vérifiée, les rôles personnalisés et l'UI launcher de la carte complète restent à implémenter séparément ; ce socle authentifie les identités par le token serveur existant.

Les enveloppes v1, les commandes existantes et leurs codes restent compatibles. Ces modules synchronisent un modèle JSON de laboratoire. Ils n'exécutent aucune construction ou interaction dans Les Sims 4, ne pilotent aucun Sim et ne prétendent pas connaître les formats propriétaires.

Le snapshot complet ajoute `zones`, `clock`, `sims`, `interactions`. La séquence globale augmente uniquement lors des mutations durables. Toutes les commandes de mutation passent sous le verrou du monde, puis par clone, journal d'idempotence et persistance avant ACK et événements. Les erreurs de stockage n'avancent aucune révision et ne diffusent aucun changement.

## Sanctions serveur — carte #128

Enveloppe WebSocket JSON **v1**, UUID de requête ; aucun acteur fourni par le client :

```json
{"v":1,"id":"UUID","type":"admin.sanction.apply","payload":{"targetPlayerId":"UUID","kind":"mute","durationSeconds":60,"reason":"Motif obligatoire"}}
{"v":1,"id":"UUID","type":"admin.sanction.revoke","payload":{"sanctionId":"UUID"}}
{"v":1,"id":"UUID","type":"admin.sanction.list","payload":{"targetPlayerId":"UUID"}}
```

`list.payload` accepte aussi `{}`. Les clés supplémentaires, doublons JSON, types invalides et versions autres que 1 sont refusés (`invalid_request`). `targetPlayerId` et `sanctionId` sont des UUID. `kind` : `warn|mute|build_suspend|kick|ban`, sensible à la casse. `reason` est un texte de 1 à 1000 caractères avant trim, obligatoire et non vide après trim. `durationSeconds`, lorsqu'il existe, est un entier entre 1 et 31536000 ; `null`, 0 et fractions sont invalides. `warn` et `kick` interdisent la durée ; `mute` et `build_suspend` l'exigent. Pour `ban`, son absence signifie permanent.

| Rôle | `sanctions` | Durée maximale | Ban permanent |
|---|---|---|---|
| player | `[]` | aucune | non |
| moderator | `["warn","mute","kick","ban"]` | 86400 secondes (24 h) | non |
| admin / owner | `["warn","mute","build_suspend","kick","ban"]` | 31536000 secondes (365 jours) | oui |

`admin.permissions.get {}` ajoute `sanctions` comme ci-dessus et `sanctionLimits:{maxDurationSeconds,permanentBan,reasonMaxLength:1000}`. Ces limites sont appliquées par le serveur, indépendamment de l'affichage launcher. Les permissions sont relues à chaque nouvelle commande. La validation syntaxique précède les contrôles ; les droits et la gravité de l'acteur sont vérifiés **avant l'existence de la cible**. Un joueur reçoit `role_forbidden` même pour une cible ou une sanction inconnue. Jamais d'application/révocation sur soi, un pair ou un supérieur. La révocation exige aussi un rôle au moins égal au rôle de l'auteur **au moment de l'application** et la gravité autorisée ; rétrograder l'auteur ne permet pas de contourner cette protection. Les sanctions existantes restent applicables si la cible change ensuite de rôle.

ACK `apply/revoke.payload` : `{sanction,sequence}`. ACK `list.payload` : `{sanctions:[...],sequence}` ; lecture sans écriture, historique retenu actif ou inactif, seulement sur des cibles de rang inférieur. Une cible explicitement demandée de rang égal/supérieur donne `role_forbidden`. Une cible autorisée inconnue donne `not_found`.

Chaque `sanction` est exactement `{sanctionId,targetPlayerId,kind,reason,actorId,actorRole,appliedAt,expiresAt,revokedAt,revokedBy,active}`. Dates ISO8601 UTC serveur ; dates/identité de révocation absentes : `null`. `warn/kick` sont des actions ponctuelles (`active:false`), non révocables. `mute/build_suspend/ban` sont actives jusqu'à révocation ou `now >= expiresAt`. Ban permanent : `expiresAt:null`. Plusieurs restrictions de même type peuvent coexister : les lever exige de révoquer chaque sanction active. Pour le refus de connexion, le ban permanent prévaut, sinon la fin la plus tardive est renvoyée.

Application serveur :

- `mute` refuse `chat` avec `muted`, avant le cache d'idempotence et toute diffusion.
- `build_suspend` refuse `build.apply` (y compris undo/redo) et `object.set` avec `build_suspended`, avant les ACL/révisions/cache. Les lectures restent disponibles.
- `kick` retire la session et la présence de zone, puis ferme son WebSocket ; reconnexion autorisée.
- `ban` fait de même et refuse le `hello` de l'identité **après authentification du token**, avec `{v:1,id,type:"error",ok:false,code:"banned",message:"banned",payload:{expiresAt:"DATE_UTC"|null}}`. Un token erroné conserve `unauthorized` et ne révèle pas le ban. Une identité bannie authentifiée conserve `banned` même si le serveur est plein.
- Aucun motif ni audit n'est diffusé dans les snapshots publics. Les changements de présence utilisent les événements `players` et `zone` existants. Une fermeture ancienne ne retire jamais une nouvelle session ; chaque commande WebSocket est liée à son UUID de session.

Codes : `role_forbidden` (droits/gravité/hiérarchie), `invalid_request` (format), `not_found` (cible ou sanction autorisée absente), `sanction_inactive` (révocation expirée/déjà levée/action ponctuelle), `sanction_limit` (1000 sanctions toutes actives), `muted`, `build_suspended`, `banned`. Codes transversaux : `unauthorized`, `storage_unavailable`, `request_id_conflict`, `request_limit`, `server_full`.

Persistance additive dans le **stockage du monde existant**, sous le même verrou/clone/commit durable : `sanctions` (dictionnaire par UUID) et journal `sanctionEvents` `{sanctionId,actorId,targetId,kind,action:"apply"|"revoke",reason,at,expiresAt}`. Le journal de sessions existant enregistre la déconnexion dans le même commit. Aucun nouveau service, aucune fausse SuguruDB. JSON testé sur disque ; aucune migration ni connexion de production. Une panne laisse audit/sanction/séquence/session inchangés. Le rejeu UUID/payload retourne l'ACK initial sans réappliquer ni redéconnecter ; contenu différent : `request_id_conflict`. Les ACK passés ne contournent pas les restrictions courantes de chat/construction.

Maximum 1000 sanctions retenues et 1000 événements d'audit : lors d'une application à capacité, seule la plus ancienne sanction **inactive** est recyclée ; aucune restriction active n'est supprimée. Expiration calculée depuis l'échéance absolue persistée, à chaque admission/commande/lecture, sans timer à reconstruire ni événement d'expiration fictif. Le serveur doit avoir une horloge UTC correcte. `revoke` conserve le motif original dans l'audit et permet unmute/unban/rétablissement construction.

Limites : identité stable = playerId + token actuels ; création d'une nouvelle identité reste possible, sans liaison Discord ni anti-évasion supplémentaire. Warning enregistré et consultable, sans UI/notification native ajoutée. Pas de suspension d'invitation, appel, gestion de preuves, frontend ou validation Sims 4 dans cette livraison serveur. **NOT_TESTED_GAME**.

## DLC déclaratifs

`DLCService.Normalize` accepte au plus **256 valeurs distinctes**, insensibles à la casse ; format inchangé `^[A-Za-z0-9_-]{1,24}$`, normalisation majuscules et tri ordinal. Une 257e valeur distincte ou un format invalide donne `invalid_request`. La même borne s'applique aux dépendances de construction persistées. Test synthétique de 116 packs (EP01–21, FP01, GP01–12, 82 SP), sans preuve de possession/licence EA ni compatibilité en jeu. Le plafond client launcher de 100 est conservé ; aucun client modifié.

## Zones

`zone.enter {lotId}`, `zone.snapshot {lotId}` retournent `{lotId,revision,items,participants,requiredDlcs}`. `zone.leave {}` retourne `{lotId:null|string}`. Un participant peut être dans une seule zone. Les participants sont des identifiants de joueurs connectés, jamais persistés ; la reconnexion doit refaire `zone.enter`. L'accès vérifie l'union des DLC du terrain et des éléments construits. Les déclarations DLC sont déclaratives, pas une preuve de licence.

Événement `zone {lotId,participants}` lors d'entrée, sortie et déconnexion. Ces commandes de présence/lecture ne sont pas journalisées durablement et sont rejouables sans effet cumulatif. Le snapshot de reconnexion contient toutes les zones mais aucune présence périmée.

## Construction

`build.apply {lotId,expectedRevision,operation:{kind,itemId?,category?,data?,requiredDlcs?:[]}}`.

- `kind`: `place`, `update`, `delete`, `undo`, `redo`.
- `place/update`: `itemId`, `category`, `data` objet JSON requis ; `requiredDlcs` facultatif.
- `delete`: `itemId` requis. `undo/redo`: aucun identifiant d'élément requis.
- Catégories acceptées : `furniture`, `wall`, `door`, `window`, `floor`, `room`, `roof`. Toute autre catégorie est refusée.
- Seul le propriétaire du terrain peut construire, sous réserve de `build_suspend` ; `expectedRevision` est la révision de sa zone. Un changement incrémente sa révision et la séquence globale. Deux changements concurrents sur la même révision donnent un succès et `stale_revision`.

ACK et événement `build` partagent `{lotId,revision,sequence,zone}`. Élément : `{itemId,category,data,requiredDlcs}`. L'historique persistant garde les 64 dernières étapes undo ; redo est effacé lors d'une nouvelle branche. Les révisions restent monotones lors d'undo/redo. Les DLC sont recomputés d'après les éléments actuels sans suppression implicite. Le propriétaire doit disposer des DLC du contenu restauré. Limites : 1000 éléments/zone, 8192 caractères JSON/élément, 256 dépendances/élément.

Le contenu courant et la somme des snapshots undo+redo ont chacun un budget JSON UTF-8 de 8 MiB par zone. Les plus anciens snapshots undo sont retirés pour respecter ce budget ; les éléments courants restent intacts. Le redo n'est jamais supprimé par cette réduction automatique. Si le redo seul dépasse le budget, l'opération est refusée avec `history_limit`, sans modifier la zone ni ses piles. Le stockage d'un modèle en deltas pourra remplacer ces snapshots pour réduire encore la mémoire ; aucun format natif de construction n'est inclus.

Codes supplémentaires : `invalid_category`, `invalid_operation`, `item_exists`, `history_empty`, `build_limit` ; codes v1 `forbidden`, `dlc_missing`, `not_found`, `stale_revision`, `storage_unavailable` réutilisés.

Si un commit construit ou restaure du contenu exigeant un DLC absent chez un participant, sa présence dans la zone est retirée après le commit et un événement `zone` inclut `reason:"dlc_missing"`. Le contenu et les foyers restent intacts ; aucune éviction ne survient en cas d'échec de persistance. Le snapshot contenu dans l'ACK `build` tient déjà compte des participants compatibles.

## Horloge

`clock.snapshot {}` retourne `{milliseconds,paused}`. L'horloge logique utilise une source monotone serveur injectable en test. Elle avance avec au moins un joueur connecté et se fige lorsqu'il n'y en a plus. Sa valeur est capturée lors des commits, checkpoints et dernière déconnexion. Le redémarrage ne rajoute aucun temps réel passé hors ligne. Aucun réglage d'heure client ni simulation du moteur n'est accepté.

## Sims abstraits

`sim.publish {simId,householdId,lotId,expectedRevision,position:{x,y,z},rotation,animation?}`. Le foyer doit appartenir à l'auteur ; seul le créateur peut réviser le Sim. L'auteur doit participer à la zone et disposer de ses DLC. Positions numériques finies bornées à ±1000000, rotation finie, animation texte 1–80 caractères (défaut `idle`). Limite 64 Sims/auteur.

ACK et événement `sim` : `{sim:{simId,ownerId,householdId,lotId,position,rotation,animation,revision},sequence}`. Codes `zone_required`, `invalid_position`, `sim_limit`. Cette pose est une donnée de réplication ; aucune application en jeu n'est implémentée.

## Interactions abstraites

`interaction.request {actorSimId,targetSimId,action}` exige propriété de l'acteur, une cible différente et deux Sims dans la zone actuelle de l'auteur. Crée `{interactionId,actorSimId,targetSimId,requesterId,targetOwnerId,lotId,action,status:"pending",revision:1}`.

`interaction.respond {interactionId,expectedRevision,status:"accepted"|"rejected"}` exige propriété de la cible, participation à la zone et état encore `pending`. ACK/événement `interaction` : `{interaction,sequence}`. Réponses révisées ne peuvent être changées ; les doublons du même ID rejouent exactement le résultat initial. Limite 1000 interactions/auteur, code `interaction_limit` ; réponse tardive `interaction_closed`.

Une acceptation signifie uniquement un accord dans le modèle réseau, jamais une action du moteur.

La cible doit être présente dans sa zone lors de la demande (`target_offline`). Lors d'une réponse, l'acteur doit rester présent et les deux Sims doivent toujours se trouver dans la zone initiale ; sinon `interaction_context_changed` est renvoyé. Le départ d'un joueur ne déclenche aucune interaction en arrière-plan.

## Manifeste mods déclaratif

`hello.payload.mods` est facultatif : `[{id,version,hash?}]`. Au maximum 256 entrées ; identifiant 100 caractères, version 64, caractères ASCII alphanumériques et `._+-` seulement, identifiants uniques insensibles à la casse ; hash facultatif SHA256 hexadécimal 64 caractères. Aucun chemin, fichier, script ou secret n'est accepté. `invalid_manifest` refuse uniquement des métadonnées malformées.

`snapshot.modManifest` expose `{advisory:true,differences:[{modId,versions,hashes,missingPlayers}]}` entre les joueurs actuellement connectés. Les différences de versions/hash/présence ne bloquent pas la connexion. Ces déclarations sont informatives : aucune détection automatique, preuve de licence, preuve d'intégrité locale ou exécution de mod n'est effectuée. La liste brute n'est pas persistée ; chaque reconnexion la redéclare.
# Présence native automatique — game.* (12 octobre 2026)

La connexion `hello` est distincte de la présence dans le jeu. Après sélection et
`character.bind` du foyer réellement chargé, le mod envoie automatiquement
`game.join`, y compris si aucun autre joueur n'est connecté. Aucun bouton de
synchronisation n'est nécessaire. Ces messages n'établissent aucune preuve
d'affichage natif des Sims distants.

`game.join` accepte exactement :

```json
{"saveId":"slot-2","nativeHouseholdId":"10","activeSimId":"11","simIds":["11"],"worldId":"1","lotId":"77"}
```

`saveId` : chaîne opaque non vide de 64 caractères maximum, sans contrôle.
Toutes les identités natives : chaînes décimales de 1 à 20 caractères ; aucun
nombre JSON flottant, booléen ou identité nulle `0`. `simIds` : 1 à 8 identités uniques, ensemble exact de
`CharacterBinding.SimIds`. Foyer identique au binding et Sim actif inclus ;
personnage sélectionné, possédé par la session et `bound` obligatoires.
`playerId` est dérivé exclusivement de la session. Champs supplémentaires ou
dupliqués refusés. Le serveur mémorise le contexte sauvegarde/foyer/Sims sans
transférer de fichier natif ni prétendre contrôler les observations du moteur.

La zone est `native:<worldId>:<lotId>` : deux mondes portant le même identifiant
de terrain restent distincts. Le terrain observé est ajouté au catalogue sans
propriétaire ni résidence revendiquée (plafond 4096 terrains). `ZoneService`
reste l'unique registre de participants. Les changements sont sauvegardés
atomiquement par `IWorldStore` (JSON ou adaptateur SuguruDB existant) avant ACK
et diffusion.

ACK : `{presence,participants,sims,sequence}`. `presence` contient `playerId`,
`characterId`, `saveId`, `nativeHouseholdId`, `activeSimId`, `simIds`, `worldId`,
`lotId`, `zoneId`, `updatedAt` UTC et `online`. `sims` ne contient que les poses
observées et publiées dans la session courante par les participants de cette
zone. Un join/rejoin/voyage ne rejoue aucune ancienne position et n'invente
aucune pose.

`game.state` accepte exactement :

```json
{"poses":[{"simId":"11","position":{"x":1,"y":2,"z":3},"rotation":0.5,"animation":"idle"}]}
```

1 à 8 poses uniques, appartenant au binding et à la présence courante.
Position finie, coordonnées absolues <= 1 000 000 ; rotation finie ; animation
optionnelle, chaîne de 1 à 80 caractères ou `null`. Une animation absente ou
non observée reste `null` dans ACK et stockage ; elle ne devient jamais `idle`.
Aucun champ besoin/humeur/argent.
Au moins une seconde entre deux publications acceptées par joueur : erreur
`state_rate_limited`. Les données réutilisent `SimReplication` et ses ACL/CAS.
ACK `{sims:[{sim,sequence}],sequence}` ; événement `game.state`
`{playerId,zoneId,data,recipientPlayerIds}` avec `data` identique à l'ACK.
Les 128 derniers ACK d'états par joueur sont mis en cache runtime uniquement ;
le dernier état est durable, sans journal illimité à 1 Hz. Après redémarrage
le client réobserve et publie, il ne rejoue pas une ancienne pose.

`game.leave` accepte `{}` et retourne `{zoneId,sequence}`. Déconnexion,
sanction kick/ban, leave et voyage retirent la présence runtime de la zone.
Le dernier contexte reste durable mais `online` est calculé en mémoire et
n'est jamais rétabli par la lecture du stockage.

Événement `game.presence` : `{presence,participants,sims,sequence,
previousZoneId,recipientPlayerIds}`. Destinataires : joueur concerné et
participants des zones nouvelle/ancienne, jamais toutes les autres zones.
Identités natives et positions restent des observations non fiables du
client, bornées et autorisées ; aucun mécanisme ne les transforme en besoins,
fonds, ownership ou simulation serveur. Erreurs spécifiques :
`character_required`, `character_not_bound`, `binding_mismatch`,
`game_join_required`, `state_rate_limited`, `lot_limit`.

Limite actuelle : la table des poses est indexée par SimId et l'exclusivité du
binding interdit le contrôle concurrent d'une même identité. Elle ne fournit
pas encore d'identité partagée à plusieurs joueurs pour un même foyer/save ;
aucune garantie de partage collaboratif de foyer n'est revendiquée ici.

# Scène native et flux de présence — développement après0.1.18

Additions rétrocompatibles, non déployées en production :

- Chaque `game.join` accepté avec une nouvelle requête crée un `streamId` UUID runtime. L'ACK et `presence` portent cet identifiant ; une répétition du même UUID de requête conserve exactement l'ACK. Le flux change au voyage/rejoin. Il ne survit pas au redémarrage ; aucune pose persistée n'est prête avant nouvelle observation.
- `game.snapshot {}` est une lecture authentifiée, réservée au joueur réellement en jeu : `{zoneId,sequence,participants:[playerId],presences:[presence avec streamId et online:true],sims:[SimState]}`. Le terrain est déterminé par sa présence serveur, aucun terrain arbitraire demandé. Pas d'écriture DB ni journal de requête ; seulement poses des participants natifs de cette zone prêts depuis leur join.
- `game.join` ACK porte aussi `presences` et les poses prêtes des autres participants. Événements `game.presence` portent `presences`, `streamId` et les listes existantes.
- `game.state` accepte optionnellement `{poses,streamId}`. Avec champ présent, une ancienne présence est refusée `stale_stream` avant mutation/cadence. Sans champ : compatibilité0.1.18, aucune garantie supplémentaire sur un contexte ancien envoyé.
- Les poses peuvent porter `level` entier de -8 à16 (routing_surface.secondary_id natif). Niveau absent : SimState.level null, aucun rez-de-chaussée inventé. Valeur illisible/hors bornes : invalid_level et refus atomique. Application native fiable impossible sans niveau connu ; le client ne doit pas supposer celui du clone local.
- Événement `game.state` conserve `{playerId,zoneId,data,recipientPlayerIds}` et ajoute `{streamId,sequence,sims:[SimState]}` plat. `data` conserve l'ACK historique `{sims:[{sim,sequence}],sequence}` et ajoute `streamId`. Pas de besoins/humeur/argent.
- Appliquer les poses seulement pour le flux courant de ce joueur annoncé dans la présence/snapshot et des révisions strictement croissantes par Sim. Un trou dans la séquence globale n'est pas forcément une perte de pose : les autres zones et événements la font avancer. Demander snapshot lors d'un contexte inconnu, avec requête en vol unique et attente bornée.
- Identifiant serveur du Sim distant et identifiant du clone local sont différents : mapping explicite requis. Une présence quittant la zone retire seulement la représentation distante validée, jamais un foyer local ni un Sim dont la propriété est incertaine.
- Voyage : départ et arrivée sont deux payloads séparés ; la notification de départ ne contient aucune pose/présence de destination. La disponibilité des poses est suivie par Sim, réinitialisée au join : republier un membre du foyer ne réexpose pas les anciennes poses des autres.

Tests serveur/transport ne prouvent pas la visibilité du Sim dans EA : **NOT_TESTED_GAME**.

# Apparence native et habitants hors ligne — game.appearance.* (12 octobre 2026)

Relayés par le pont du launcher et autorisés dans le mod. Mémoire serveur seulement (republiée à chaque connexion).

- `game.appearance.publish {simId, profile}` → `{sha256, changed}`. `profile` = `{version:1, format:"simdata-allowlist-v1", game, sha256, data}` (base64 ≤ 58 668 caractères, soit 44 000 octets). Identique → `changed:false`, aucune diffusion. Sinon diffusion à tous de l'événement `game.appearance {playerId, simId, profile}`. Codes : `invalid_request`, `profile_shape`, `profile_version`, `profile_size`, `profile_malformed`, `profile_hash`, `appearance_rate_limited` (10 s), `appearance_full` (64).
- `game.appearance.list {}` → `{appearances:[{playerId, simId, sha256}]}` (sans profil).
- `game.appearance.get {playerId}` → même forme que l'événement, ou `appearance_unknown`.
- `snapshot.residents` : `[{playerId, name}]` des propriétaires de terrain, connectés ou non (nom du foyer « Nom (S4T) » d'un ami hors ligne).

Côté mod (`appearance_sync.py`, `remote_household.py`) : chaque autre joueur devient, dans la partie liée seulement, un foyer natif « Nom (S4T) » (description `S4T:<playerId>:<sha16>`), créé par la séquence de clonage du jeu, qui emménage dans sa maison résolue par `home_lots` si elle est libre. NOT_TESTED_GAME.
