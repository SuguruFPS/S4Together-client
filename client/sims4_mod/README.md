# Mod S4Together 0.2.0 Alpha

Source indépendante. Commandes enregistrées au chargement de `s4t` : diagnostic, connect, poll, status, disconnect, observe, chat, snapshot, retry. La connexion stdlib WebSocket parle au serveur réel ; les observations lisent uniquement les identifiants terrain/Sim actif sur le thread principal. Aucun setter de gameplay, création de Sim ou accès aux sauvegardes.

`python tools/build_mod.py` produit un ZIP de sources nommé `.ts4script`, accompagné de `mod-readiness.json` (Python du constructeur, magic number, SHA-256). Par défaut ce prototype ne contient pas de bytecode de jeu. `--target-python CHEMIN_PYTHON` permet une compilation explicite avec un interpréteur cible identifié. Le Python embarqué et le patch cible sont inconnus : **chargement en jeu non vérifié**. L'extension de l'archive ne constitue pas une preuve de compatibilité. Aucun fichier installé dans Mods ni sauvegardes.

Procédure complète : `docs/SIMS4_INTEGRATION.md`. WS en clair limité au loopback ; utiliser WSS avec certificat vérifié pour un serveur LAN/distant. Aucun bypass TLS. Configuration et identités sous `%LOCALAPPDATA%\S4Together\mod`, sans token dans console ou journaux. Les observations reçues restent dans le mod ; elles n'écrasent jamais l'état Sims.
