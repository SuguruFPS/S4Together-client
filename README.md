# Client de test0.1.48

Client Windows de test0.1.48

- Protection supplementaire contre les invitations natives de copies de joueurs : veto au clic, au picker et a la creation de situation, avant les effets connus. Vrais PNJ conserves. Aucun voyage force.
- Correctifs installation et reprise des meubles47 conserves.
- Verification :1302PythonPASS/1SKIP, couverture93.07%,186serveur,12checks paquet et vrai remplacement/demarrageWPF48 profil isole PASS.

NOT_TESTED_GAME : autres voies natives et invitations authentifiees owner vers destinataire restent ouvertes. Un ancien noeud refuse peut rester actif jusqu'au nettoyage natif du service ; nettoyage sans effets non prouve. Pas de nouvelle correction de maison complete Hatay, des meubles manquants, de l'heure/meteo/vitesse ou des conversations. Aucun serveur/base/save joueur modifie.

---
# Candidat client Windows 0.1.47

Installation du mod transactionnelle et reprise limitee de l application des objets de terrain apres echec temporaire. Diagnostics automatiques de propriete/lecture/application. La protection Defender reste active ; une autorisation Windows personnelle peut etre necessaire.

1301tests Python PASS/1SKIP,186serveur, paquet et vrai demarrage WPF PASS. Rendu meubles NOT_TESTED_GAME ; maison complete Hatay, murs/sols/toits et synchronisation generale des actions restent NON corriges. Aucune sauvegarde de joueur ni production modifiee.

---
# Client0.1.46 — continuite du proxy et essai assise

Correction du gel lors d un changement de tenue pour le meme Sim deja prouve. Premier rejeu borne de l assise utilisateur sur proxy verifie et siege present/non ambigu. Aucun rejeu social, mouvement force du vrai joueur ou partage de besoins. Diagnostic mobilier automatique affine.

104launcher/186serveur/1298PythonPASS1SKIP, paquet et WPF PASS. NOT_TESTED_GAME : assise et continuite a verifier sur deux jeux. Meubles/batiments manquants, conversations, anciens clones non prouves et heure/meteo/vitesse restent ouverts. Compatible serveur0.1.33, aucun serveur remplace.

---
# Client0.1.45 — portraits transparents

Le blanc relie aux bords des portraits devient transparent uniquement a l affichage. Originaux preserves ; les details blancs internes restent visibles. Fonds colores non detoures.104tests launcher et paquet/WPF PASS. Rendu sur portraits joueurs a confirmer. Serveur0.1.33 inchange.

---
# Client Windows de test 0.1.44

Favoris en tete, catalogue public volontaire et connexion privee centrale. SuguruSims est inscrit volontairement dans le catalogue public ; aucune adresse LAN publiee. Detection de coupure, pause du foyer lie et reprise des fonds confirmes ; aucune restauration complete de sauvegarde. Mods Garder/Retirer et cartes transparentes conserves.

Avec serveur0.1.33 : bail de session15s et cadence250ms negociee ; anciens serveurs compatibles a1Hz. Serveur SuguruSims0.1.33 active sur accord du proprietaire. NOT_TESTED_GAME : pause native, animations, meubles/maisons et heure/meteo/vitesse restent a valider ou corriger.

102tests launcher,186serveur,1271Python PASS/1SKIP ; paquet et vrai demarrage WPF PASS.

---
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
