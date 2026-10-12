"""Paquet client autonome, sans données runtime ni secrets ; aucun déploiement natif."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import zipfile
from build_mod import build, ROOT

LAUNCHER=ROOT/'launcher'/'S4Together.Launcher'

def publish_launcher(output):
    # Exécutable unique autonome : le joueur n'installe pas .NET. Paquets runtime pris dans le cache NuGet local.
    command=['powershell','-NoProfile','-ExecutionPolicy','Bypass','-File',str(ROOT/'tools'/'dotnet.ps1'),'publish',str(LAUNCHER),
             '-c','Release','-r','win-x64','--self-contained','true','-p:PublishSingleFile=true',
             '-p:IncludeNativeLibrariesForSelfExtract=true','-p:EnableCompressionInSingleFile=true','-p:DebugType=none','-o',str(output)]
    subprocess.check_call(command,stdout=subprocess.DEVNULL)
    exe=output/'S4Together.exe'
    assert exe.is_file(),'publication du launcher absente'
    return exe

def package(target_python, output=None, archive=None):
    if output is not None:
        output=output.resolve()
        if not output.is_relative_to((ROOT/'artifacts').resolve()) or output == (ROOT/'artifacts').resolve():
            raise ValueError('Le staging doit être un sous-dossier artifacts du worktree.')
        if output.exists():
            raise ValueError('Choisir un dossier staging neuf ; aucun staging explicite ne sera supprimé.')
    else:
        output=ROOT/'artifacts/game-client-test'
    archive=archive.resolve() if archive is not None else ROOT/'S4Together-GameClient-Test.zip'
    if archive.exists() and archive != ROOT/'S4Together-GameClient-Test.zip':
        raise ValueError('Archive staging déjà présente ; aucun paquet existant ne sera remplacé.')
    if output.exists():shutil.rmtree(output)
    mod=build(output/'mod',target_python)
    readiness=json.loads((output/'mod/mod-readiness.json').read_text(encoding='utf-8'))
    exe=publish_launcher(output/'launcher')
    stage=output/'package'
    (stage/'mod').mkdir(parents=True)
    shutil.copyfile(exe,stage/exe.name)
    # Disposition lue par ModInstaller.LoadPackage : mod\<script> + mod\mod-readiness.json (sha256, version).
    shutil.copyfile(mod,stage/'mod'/'S4Together-GameClient.ts4script')
    readiness['artifact']='S4Together-GameClient.ts4script'
    (stage/'mod'/'mod-readiness.json').write_text(json.dumps(readiness,indent=2)+'\n',encoding='utf-8')
    shutil.copyfile(ROOT/'client'/'launcher_package'/'LIRE-MOI.txt',stage/'LIRE-MOI.txt')
    (stage/'VERSION.txt').write_text(readiness['version']+'\nNOT_TESTED_GAME\n',encoding='utf-8')
    hashes=[]
    for file in sorted(stage.rglob('*')):
        if file.is_file() and file.name!='SHA256SUMS.txt':
            hashes.append(hashlib.sha256(file.read_bytes()).hexdigest()+'  '+file.relative_to(stage).as_posix())
    (stage/'SHA256SUMS.txt').write_text('\n'.join(hashes)+'\n',encoding='utf-8')
    archive.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive,'w',zipfile.ZIP_DEFLATED) as zip:
        for file in sorted(stage.rglob('*')):
            if file.is_file():zip.write(file,file.relative_to(stage).as_posix())
    digest=hashlib.sha256(archive.read_bytes()).hexdigest()
    archive.with_suffix(archive.suffix+'.sha256').write_text(digest+'  '+archive.name+'\n',encoding='ascii')
    with zipfile.ZipFile(archive) as zip:
        assert zip.testzip() is None
        for entry in hashes:
            expected,name=entry.split('  ',1)
            assert hashlib.sha256(zip.read(name)).hexdigest()==expected
        assert json.loads(zip.read('mod/mod-readiness.json'))['sha256']==hashlib.sha256(zip.read('mod/S4Together-GameClient.ts4script')).hexdigest()
    print(json.dumps({'archive':str(archive),'sha256':digest,'files':len(hashes)+1,'status':'NOT_TESTED_GAME'}))

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--target-python',type=Path,required=True)
    parser.add_argument('--output',type=Path,help='Staging neuf dans artifacts ; ne remplace pas le staging courant.')
    parser.add_argument('--archive',type=Path,help='Archive distincte ; refuse le remplacement d’un paquet existant.')
    args=parser.parse_args()
    package(args.target_python,args.output,args.archive)
