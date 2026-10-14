# S4Together — client (launcher + mod)

Sources publiques du launcher Windows et du mod Sims 4 de S4Together, projet multijoueur expérimental.
Le serveur n'est pas inclus.

Sources actuelles : **client 0.1.39**, correction du retour à l’ancienne version après mise à jour : confirmation de démarrage atomique, compatible avec le helper0.1.35. Le mod suivi intact est actualisé automatiquement, jeu fermé avec sauvegarde.89tests launcher et véritables remplacements WPF avec helpers35 et39 PASS. Installation sur PC distant encore à confirmer.
La version téléchargeable est celle de releases/latest. Le rendu mutuel, le jour natif et la latence <5 s restent à vérifier dans deux vrais jeux. Murs, sols, toits et relief non synchronisés.
La mise à jour automatique suit les releases publiées, pas les commits de cette branche.

**Télécharger :** [dernière release](https://github.com/SuguruFPS/S4Together-client/releases/latest) →
`S4Together-Client-win-x64.zip`, décompresser hors du dossier Mods, lancer `S4Together.exe`.

Le launcher se met à jour tout seul à chaque démarrage depuis les releases de ce dépôt
(empreinte SHA256 vérifiée, retour à l'ancienne version si la nouvelle ne démarre pas).

État : version de test, **non validée dans Les Sims 4** (NOT_TESTED_GAME). Voir `client/launcher_package/LIRE-MOI.txt`.
