Candidat client0.1.56 local : protections des transitions Gérer les mondes/reprise et domicile des copies résidentes déjà enregistrées.1435tests Python PASS/1SKIP,12checks paquetPASS. Serveur0.1.39 déployé avec reprise cohérente des propres commits confirmés en base. Aucune nouvelle release : auto-update publique reste0.1.53. Cause exacte erreur102/renduAna nonprouvés : NOT_TESTED_GAME.

Candidat client0.1.55 : filtre des imports solo (foyer actif, animaux sauvegardés et foyers déjà liés). Code disponible, aucune release publiée ; dernière mise à jour automatique0.1.53.113tests launcher PASS,2tests de remplacement bloqués par jeu ouvert ;12contrôles paquet PASS. NOT_TESTED_GAME.

Publication0.1.54 en attente de confirmation explicite Citadel. Derniere release automatique :0.1.53. Serveur0.1.37 installe et verifie.

# Client de test0.1.54

Client0.1.54 — reprise des meubles et actions

Assise et télévision : ciblage du meuble ou de sa partie exacte, capacité additive serveur37.
Reprise espacée des meubles et trajets après incident, sans doublon dans les fixtures ni téléportation.
Garde de chargement et libération des résidences avant voyage ; copies des joueurs protégées.

Serveur0.1.37 installé après sauvegarde : journal des requêtes compressé sans perte àla prochaine
écriture normale, reprise après incident par lecture/version/empreinte sans écrasement aveugle.

Vérifié hors jeu :1424PythonPASS/1SKIP,200groupes serveur autonome,112launcher,
12checks paquet et vrai remplacementWPF54. Archive vérifiée sur vraieSuguruDB HTTP/SQLite
isolée après crash/sauvegarde/restauration :1100UUID conservés. Gitleaks : aucune fuite.

Fermer les jeux, actualiser les deux clients/mods puis rejoindre les personnages déjà liés.
Visiter normalement, comparer les meubles puis marcher, s'asseoir et regarder laTV.
Aucune commande nécessaire. Rendu et gameplay : NOT_TESTED_GAME.

Restent ouverts : cause exacte erreur109, maison complèteHatay/murs/sols/toits,
chien/foyer complet, heure/météo/vitesse, sport et conversations complets, mods tiers.
Cette version ne doit pas être présentée comme une synchronisation complète validée.

---
# Client de test 0.1.53

Client 0.1.53 — meubles et déplacements distants

Correctif du blocage constaté chez Umupafa : les objets entièrement lus peuvent être publiés même si le relevé des contours de pièces échoue. Le serveur 0.1.36 conserve les derniers contours complets séparément. Le visiteur applique uniquement les objets sur la copie du terrain autorisée. Les DLC/CC doivent être possédés et installés localement ; aucun contenu de pack n'est distribué.

Les copies de Sims ne restent plus indéfiniment bloquées sur le seul drapeau moteur « moving » : la progression réelle est contrôlée, avec reprises bornées. Un trajet S4Together remplace son précédent trajet au lieu de s'accumuler. Aucun TP ni contrôle du Sim jouable.

Vérifié hors jeu :1396 Python PASS/1 SKIP,360 tests ciblés,112 launcher,195 serveur autonome,5 suites unitaires terrain,12 checks paquet et vrai remplacement WPF53 PASS. Vraie SuguruDB HTTP/SQLite : objets seuls et conservation des contours relus après réouverture.

Fermer les jeux, actualiser les deux clients/mods, rejoindre avec les personnages liés. Umupafa reste dans sa maison pour sa publication automatique, puis Suguru visite son terrain. Comparer les meubles DLC et les déplacements dans les deux sens. Aucune commande nécessaire.

Rendu mobilier/DLC, mouvement réel et latence : NOT_TESTED_GAME. La maison complète de Hatay (murs, sols, toits) n'est pas reconstruite par ce correctif.

---
# Client de test 0.1.52

Client 0.1.52 — sauvegarde multijoueur envoyée automatiquement

Après chargement du personnage lié et un enregistrement normal dans Les Sims 4, le launcher envoie une copie stable de la partie complète au serveur 0.1.35. Le slot réellement chargé est vérifié ; les sauvegardes originales restent intactes. Pas de commande à lancer.

Deux versions privées sont conservées par personnage. La copie contient les données du monde et des foyers : l'administrateur du serveur y accède, sans téléchargement public ni partage aux autres joueurs. Taille maximale 256 Mio. L'envoi est authentifié, contrôlé par hash et réessayé automatiquement en cas d'échec.

Ce transfert permet de récupérer les données natives des maisons. Il n'applique pas les sauvegardes aux autres parties et ne corrige pas encore l'affichage de la maison de Hatay. La progression non enregistrée par le jeu n'est pas incluse.

Vérifications :1390 Python PASS/1 SKIP,112 launcher PASS,195 serveur autonome PASS,2 tests unitaires stockage/auth,12 checks paquet et vrai remplacement WPF52 PASS. Transfert HTTP réel testé uniquement en fixture isolée. Depuis le vrai jeu et rendu des maisons : NOT_TESTED_GAME.

Test : mettre à jour les clients et le mod, rejoindre avec le personnage lié, charger la partie multijoueur puis enregistrer normalement. Garder le launcher ouvert environ une minute. Le statut confirme l'envoi de la copie au serveur privé.

---
# Client de test0.1.51

Client Windows de test 0.1.51 — résidence Analia

Correctif expérimental de résidence canonique : un PNJ strictement non joué, non sélectionnable et non instancié peut libérer l'adresse attribuée au foyer d'un joueur. Le PNJ est conservé intact, temporairement sans domicile, avec journal local durable de son adresse avant modification. Aucun foyer joué, joueur ou copie S4T n'est expulsé. Si l'installation échoue, restauration seulement si le terrain reste libre ; journal conservé sinon. Retour possible du PNJ après départ du claim et seulement si sa situation le permet.

Les deux entrées natives d'emménagement sont filtrées pour protéger les résidences serveur et empêcher les copies de déménager toutes seules. La garde se désactive hors liaison, en solo ou à l'arrêt. L'adresse native est relue avant de confirmer le succès. Aucune nouvelle version serveur nécessaire : serveur0.1.34 compatible et inchangé.

Vérifications hors jeu :1341PythonPASS/1SKIP/92.82%,194serveur,110launcher,12checks paquet et vrai remplacementWPF51PASS. Codex seul. Aucun .save personnel modifié par les outils de développement ; le mod effectue les changements de résidence dans la partie multijoueur liée, enregistrés ensuite par le jeu.

LIMITES : résultat Analia et garde dans deux vrais jeux NOT_TESTED_GAME. Les foyers joués/instanciés ou identités incertaines restent refusés. Changement de slot/foyer actif : journal de l'ancien contexte conservé, reprise non automatique.
MAISON COMPLÈTE HATAY TOUJOURS NON CORRIGÉE : murs, sols et toits non transportés. La sonde automatique décrit maintenant les méthodes des classes natives Lot/Zone sans les appeler. Il faut analyser ce nouveau relevé réel avant de définir un import sûr ; aucun chargement d'octets opaques ni fusion de sauvegardes tenté.

Test sans commande : fermer les jeux, actualiser les deux clients/mods, charger les parties liées puis rouvrir la carte après quelques secondes. Vérifier la résidence d'Analia et un déménagement vers une maison libre. Conserver les PNJ et journaux. Pour Hatay, le chargement de sa maison génère automatiquement native-probe.json enrichi et visibility-last.json dans les logs.

---
# Client de test0.1.50

Client Windows de test 0.1.50 — déménagement

- Corrige le rejet de property.move par le transport Python du mod. Le changement de domicile reste atomique et soumis aux droits du serveur.
- Confirmation perdue : même UUID et corps rejoués après5s. Snapshot explicite après confirmation ; attend la nouvelle résidence avant une autre mutation.
- Une nouvelle destination peut être retentée après trois échecs vers l'ancienne maison ; aucun foyer dupliqué ou occupant natif expulsé.
- Vérifications :1321PythonPASS/1SKIP,194serveur,110launcher,12checks paquet et vrai remplacementWPF50PASS.

LIMITES OUVERTES : résidence Analia bloquée si la destination est occupée par un foyer natif ; maison complète de Hatay (murs/sols/toits) NON transférée. Ce paquet ne corrige pas ces deux symptômes. Rendu sur deux vrais jeux et rafraîchissement de la carte NOT_TESTED_GAME. Aucun serveur/base/sauvegarde personnelle modifié ; serveur0.1.34 compatible. Les protections sauvegardes49 sont conservées.

Test sans commande : actualiser les deux clients puis lancer leurs parties multijoueurs liées. Après un déménagement vers une maison libre et un enregistrement, vérifier ancien/nouveau domicile du foyer S4T sur l'autre PC, puis reconnexion. Diagnostics automatiques ; ne supprimer aucun foyer existant pour contourner un blocage.

---
# Client de test0.1.49

Client Windows de test0.1.49 — protection des sauvegardes

- Checkpoints des changements d'argent toutes15secondes environ, reprise apres indisponibilite temporaire et rejeu du meme UUID si ACK perdu.
- Copies locales des parties deja enregistrees correspondant aux IDs du personnage lie :10versions par serveur/personnage, hash et DBPF verifies, originaux intacts, plafond2Go. Parametres > Ouvrir mes copies de securite.
-1314PythonPASS/1SKIP,194serveur,110launcher,12checks paquet et vraiWPF49r3 PASS.

Limites : ces copies ne forcent pas Les Sims4 a enregistrer les actions encore en memoire. Le checkpoint serveur ne couvre pas competences/inventaires/besoins. Restauration complete guidee et autosave native coordonnee restent a developper ; aucune garantie zero perte, NOT_TESTED_GAME. Launcher ouvert requis pour les copies. Aucun serveur/base/save personnelle modifie par cette livraison ; serveur34 reste compatible, vitesse experimentale desactivee. Meubles, maisons et heure/meteo ne sont pas declares corriges.

---
## Developpement non publie apres 0.1.48

Prototype de vitesse native automatique, inactif sans capacite serveur explicite.
Les controles de session/flux et les pauses prioritaires du jeu sont conserves.
NOT_TESTED_GAME : horloge globale et independance des lots non prouvees ; ne pas activer en production.
La derniere release telechargeable reste 0.1.48. Pas de nouvelle correction annoncee pour les meubles,
maisons completes, heure/meteo ou conversations.

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
