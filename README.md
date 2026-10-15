# Client de test 0.1.43

Mods du serveur installes automatiquement jeu ferme. Au depart : Garder ou Retirer ; retrait differe et repris au prochain lancement. Parametres permet de gerer ce choix. Fichiers personnels/modifies preserves. Cadres des portraits transparents.

Necessite un serveur avec catalogue publie ; dossier automatique disponible dans le candidat serveur0.1.32, non deploye sur la production31. NOT_TESTED_GAME : chargement reel des mods et nouveaux dialogues a valider. Animations distantes, meubles/maisons et heure/meteo/vitesse restent ouverts.

---
Historique :

# Client de test 0.1.42

Reprise automatique de la vue perdue, copies S4T passives (besoins locaux de base et sous-actions autonomes), refus de domicile tiers visible.

NOT_TESTED_GAME : animations assises/actions distantes, meubles manquants, maisons completes et heure/meteo/vitesse du proprietaire restent a corriger. Cette version ne resout pas toute la synchronisation. Fermer le jeu puis actualiser les deux launchers pour mettre le mod suivi a jour.

---
Historique du depot :

# S4Together — client (launcher + mod)

Sources publiques du launcher Windows et du mod Sims 4 de S4Together, projet multijoueur expérimental.
Le serveur n'est pas inclus.

Sources actuelles : **client 0.1.41**, réception réseau250ms, protection FullAutonomy des proxies et objets sur supports natifs compatibles.1247tests Python et89launcher PASS ; latence réelle et rendu non validés. Heure/météo et animations distantes PAS encore synchronisées. Correctifs updater39 et rechargement40 conservés.
La version téléchargeable est celle de releases/latest. Le rendu mutuel, le jour natif et la latence <5 s restent à vérifier dans deux vrais jeux. Murs, sols, toits et relief non synchronisés.
La mise à jour automatique suit les releases publiées, pas les commits de cette branche.

**Télécharger :** [dernière release](https://github.com/SuguruFPS/S4Together-client/releases/latest) →
`S4Together-Client-win-x64.zip`, décompresser hors du dossier Mods, lancer `S4Together.exe`.

Le launcher se met à jour tout seul à chaque démarrage depuis les releases de ce dépôt
(empreinte SHA256 vérifiée, retour à l'ancienne version si la nouvelle ne démarre pas).

État : version de test, **non validée dans Les Sims 4** (NOT_TESTED_GAME). Voir `client/launcher_package/LIRE-MOI.txt`.
