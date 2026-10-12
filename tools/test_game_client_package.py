"""Contrôle du ZIP joueur (launcher WPF v2) : contenu exact, empreintes, versions, fonctions attendues.

L'installation, la désinstallation et le pont sont testés par launcher/S4Together.Launcher.Tests ;
ici on vérifie seulement que le ZIP livré contient bien ce qui a été testé. Aucun test du vrai jeu.
"""
import argparse
import hashlib
import json
from pathlib import Path
import zipfile
ROOT=Path(__file__).resolve().parents[1]
EXPECTED={'LIRE-MOI.txt','VERSION.txt','SHA256SUMS.txt','S4Together.exe','mod/S4Together-GameClient.ts4script','mod/mod-readiness.json'}
# Chaînes du launcher compilé (UTF-16 dans les métadonnées .NET) : sanctions Admin, plafond DLC, intention Jouer, saison #138, personnages T3.
EXE_MARKERS=['Sanction appliquée','sont annoncés au serveur','Nouvelle partie','play_intent','season.choose','Saison du monde','character.create','character.select','Ma bibliothèque']
MOD_MARKERS=['s4t/play_intent.py','s4t/autoconnect.py','s4t/character_sync.py','s4t_loader']

def present(data,text):
    return text.encode('utf-16-le') in data or text.encode('utf-8') in data

def test(archive_path=None,published=None,report_path=None):
    archive_path=archive_path or ROOT/'S4Together-GameClient-Test.zip'
    with zipfile.ZipFile(archive_path) as archive:
        names={n for n in archive.namelist() if not n.endswith('/')}
        assert names==EXPECTED,sorted(names^EXPECTED)
        read=lambda n:archive.read(n)
        version=read('VERSION.txt').decode('utf-8').split()[0]
        sums={}
        for line in read('SHA256SUMS.txt').decode('utf-8').splitlines():
            digest,name=line.split('  ',1);sums[name]=digest
        assert set(sums)==EXPECTED-{'SHA256SUMS.txt'},sorted(sums)
        for name,digest in sums.items():
            assert hashlib.sha256(read(name)).hexdigest()==digest,name
        assert read('LIRE-MOI.txt').decode('utf-8').startswith('S4Together '+version)
        readiness=json.loads(read('mod/mod-readiness.json'))
        assert readiness['version']==version and readiness['status']=='NOT_TESTED_GAME' and readiness['loadable_claim'] is False
        assert readiness['target_compiler']['version'][:2]==[3,7]
        with zipfile.ZipFile(archive.open('mod/S4Together-GameClient.ts4script')) as mod:
            mod_names=set(mod.namelist())
            missing=[m for m in MOD_MARKERS if not any(n.startswith(m) for n in mod_names)]
            assert not missing,missing
            assert any(n.endswith('.pyc') for n in mod_names)
            commands=mod.read('s4t/commands.py').decode('utf-8')
            assert "'s4t.character'" in commands and 'genealogy' in mod.read('s4t/character_sync.py').decode('utf-8')
        exe=read('S4Together.exe')
        assert exe[:2]==b'MZ'
        # Un single-file WPF sans ces entrées natives peut fonctionner dans le dossier publish
        # puis échouer sur le PC joueur, où le ZIP ne contient que l'exécutable.
        for native in ('D3DCompiler_47_cor3.dll','PenImc_cor3.dll','PresentationNative_cor3.dll','vcruntime140_cor3.dll','wpfgfx_cor3.dll'):
            assert native.encode('ascii') in exe, 'bibliothèque WPF absente du bundle : '+native
    # Le single-file est compressé : on lit les assemblies de la même publication, plus récentes que
    # 15 minutes avant l'exe publié, et l'exe du ZIP doit être identique à celui de cette publication.
    published=published or ROOT/'artifacts/game-client-test/launcher/S4Together.exe'
    assert published.read_bytes()==exe,'exe du ZIP différent de la dernière publication'
    binaries=ROOT/'launcher/S4Together.Launcher/bin/Release/net10.0-windows/win-x64'
    assemblies=[binaries/'S4Together.dll',binaries/'S4Together.Launcher.Core.dll']
    for assembly in assemblies:
        assert 0<=published.stat().st_mtime-assembly.stat().st_mtime<900,assembly
    data=b''.join(a.read_bytes() for a in assemblies)
    absent=[m for m in EXE_MARKERS if not present(data,m)]
    assert not absent,absent
    report={'status':'PASS','sha256':hashlib.sha256(archive_path.read_bytes()).hexdigest(),'version':version,
            'checks':['exact_file_list','sha256sums','readme_version','mod_readiness_not_tested_game','mod_py37_bytecode','mod_play_intent','exe_admin_sanctions','exe_dlc_cap_256','exe_play_intent','exe_season_138','exe_characters_t3','mod_character_sync_t3'],
            'native_game':'NOT_TESTED_GAME','windows_ui_rendered':False}
    (report_path or ROOT/'tests/results/game-client-package.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(report))
if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--archive',type=Path)
    parser.add_argument('--published-exe',type=Path)
    parser.add_argument('--report',type=Path)
    args=parser.parse_args()
    test(args.archive,args.published_exe,args.report)
