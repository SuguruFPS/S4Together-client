"""Construit un ZIP source diagnostic ; ne l'installe jamais dans le jeu."""
import argparse
import ast
import hashlib
import importlib.util
import json
from pathlib import Path
import platform
import subprocess
import sys
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def _mod_version(source):
    # Version lue dans le source du mod (journal.VERSION), sans l'importer.
    import re
    text = (source / 's4t' / 'journal.py').read_text(encoding='utf-8')
    return re.search(r"^VERSION = '([^']+)'", text, re.M).group(1)


def build(output, target_python=None):
    source = ROOT / "client" / "sims4_mod"
    output.mkdir(parents=True, exist_ok=True)
    target = output / "S4Together-diagnostic-prototype.ts4script"
    files = sorted(source.rglob("*.py"))
    if not files:
        raise RuntimeError("Sources diagnostic absentes")
    for path in files:
        text = path.read_text(encoding="utf-8")
        compile(text, str(path), "exec")
        if sys.version_info >= (3, 8):
            ast.parse(text, filename=str(path), feature_version=7)
    target_info = None
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in files:
            archive.write(path, path.relative_to(source).as_posix())
        if target_python:
            inspect = "import json,sys,importlib.util; print(json.dumps({'version':list(sys.version_info[:3]),'magic':importlib.util.MAGIC_NUMBER.hex()}))"
            target_info = json.loads(subprocess.check_output([str(target_python), '-c', inspect], universal_newlines=True))
            if tuple(target_info['version'][:2]) < (3, 7):
                raise ValueError('Interpréteur cible Python 3.7+ requis')
            with tempfile.TemporaryDirectory(prefix='s4t-mod-compile-') as directory:
                for index, path in enumerate(files):
                    bytecode = Path(directory) / (str(index) + '.pyc')
                    compiler = "import py_compile,sys; py_compile.compile(sys.argv[1],cfile=sys.argv[2],doraise=True)"
                    subprocess.check_call([str(target_python), '-c', compiler, str(path), str(bytecode)])
                    archive.write(bytecode, path.relative_to(source).with_suffix('.pyc').as_posix())
    readiness = {
        "artifact": target.name,
        "version": _mod_version(source),
        "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
        "builder_python": platform.python_version(),
        "builder_magic_hex": importlib.util.MAGIC_NUMBER.hex(),
        "game_python": "UNKNOWN",
        "game_patch": "UNKNOWN",
        "contains_bytecode": target_info is not None,
        "target_compiler": target_info,
        "format": "ZIP_SOURCE_PROTOTYPE" if target_info is None else "ZIP_SOURCE_AND_TARGET_BYTECODE",
        "status": "NOT_TESTED_GAME",
        "loadable_claim": False,
        "installation_performed": False,
        "entrypoint": "s4t_loader -> s4t.bootstrap -> s4t.commands",
        "source_files": len(files),
    }
    (output / "mod-readiness.json").write_text(json.dumps(readiness, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(readiness, ensure_ascii=False))
    return target


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "artifacts" / "mod")
    parser.add_argument("--target-python", type=Path, help="Interpréteur cible identifié, pas une preuve de version du jeu")
    args = parser.parse_args()
    build(args.output.resolve(), args.target_python)
