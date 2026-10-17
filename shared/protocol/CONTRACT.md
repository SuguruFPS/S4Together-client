# Contrat Alpha 1

Extension experimentale de vitesse native (desactivee par defaut) : voir
`docs/NATIVE_SPEED_SPIKE.md`. `game.speed.get/set` exigent le stream de presence courant ;
le claim du LotId natif, jamais l'alias zone ni un champ client, donne le droit de controle.
Sans capacite snapshot `nativeSpeed:1`, le mod ne touche pas a la vitesse du jeu.

WebSocket `/ws`, JSON UTF-8, enveloppe requête `{v:1,id:"UUID",type:"hello",payload:{...}}`.
Réponse directe `{v:1,id,type:"ack",ok:true,payload:{...}}` ou `{v:1,id,type:"error",ok:false,code:"...",message:"..."}`.
Événement `{v:1,type:"players"|"chat"|"object"|"snapshot",payload:{...}}`.
`hello` payload `{name,playerId?,token?,dlcs:["EP01"]}`. Réponse payload `{playerId,token,sessionId,snapshot}`. Serveur crée identité/token aléatoire ; reconnexion exige les deux. Snapshot `{worldId,sequence,players:[{playerId,name}],households:[{id,ownerId,name}],lots:[{id,name,ownerId:null|string,householdId:null|string,requiredDlcs:[]}],objects:[{id,ownerId,value,revision}],...}` ; aucun token public.
`snapshot` {}, `household.create` {name}, `household.select` {householdId}, `property.claim` {lotId,householdId}, `property.release` {lotId}, `visit` {lotId}, `chat` {text}, `object.set` {objectId,value,expectedRevision}, `ping` {}, `ack` {sequence}.
Create household ack payload `{householdId}`. Object created by first setter; only owner can change. Claims require own household. Visit checks DLC. Object success payload `{objectId,revision,sequence}` and broadcast object `{id,ownerId,value,revision,sequence}`. Duplicated request IDs replay exact original result without reapplying (persistent per player); conflicting reuse rejects. Stale revision => `stale_revision`. Claim conflict => `lot_taken`; bad owner => `forbidden`; missing DLC => `dlc_missing`; full => `server_full`; auth => `unauthorized`; storage => `storage_unavailable`. After hello, server emits players. Every reconnect returns complete snapshot. All clients serialize sends; receiver routes by id and separately handles events.

Configuration server/appsettings.json `Server:{Host:"127.0.0.1",Port:27015,MaxPlayers:4,DataPath:"data/world.json",IdleTimeoutSeconds:60,HelloTimeoutSeconds:10}`. ENV `Server__*` overrides. HTTP `/health` public minimal; `/` serves test client. Admin console `players`, `status`, `save`, `kick PLAYERID`, `stop`. Errors logged without tokens. JSON local storage single process lock, write temp + flush + atomic replace before state commit/ACK; exclusivity and dedup under one lock. SuguruDB adapter must not invent unsupported API.

Déduplication persistante uniquement mutations, chat et visit. Lectures snapshot/ping/ack et sélection de foyer restent sans écriture disque ; sélection de foyer est un état de session. ACK exige `0 <= sequence <= current`. Quotas Alpha : 256 identités monde, 16 foyers et 1000 objets par joueur, 10000 requêtes persistantes par joueur ; au-delà rejet explicite sans éviction afin de préserver l'anti-rejeu. Une résidence principale maximum par foyer (`residence_exists`). Maximum 16 sockets simultanés en attente/session, timeout HELLO absolu. Les tokens de reconnexion du client navigateur sont locaux et spécifiques au serveur, à traiter comme secrets.
