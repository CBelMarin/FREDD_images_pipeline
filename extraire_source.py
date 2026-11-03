# -*- coding: utf-8 -*-
"""
extraire_source.py : extrait UN fichier depuis un chemin_source du pipeline ('a.zip :: b.zip :: x/y.e2e').
Usage :  python extraire_source.py "<chemin_source>" --dest <dossier>
"""
import argparse
import shutil
import tempfile
import zipfile
from pathlib import Path, PurePosixPath


def extraire(chemin, dest):
    seg = chemin.split(" :: ")
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    if len(seg) == 1:
        sortie = dest / Path(seg[0]).name
        shutil.copyfile(seg[0], sortie)
        return sortie
    tmp = Path(tempfile.mkdtemp(dir=dest))
    archive = seg[0]
    for i, nom in enumerate(seg[1:]):
        with zipfile.ZipFile(archive) as z:
            if i == len(seg) - 2:  # dernier element : le fichier cherche
                sortie = dest / PurePosixPath(nom).name
                with z.open(nom) as src, open(sortie, "wb") as out:
                    shutil.copyfileobj(src, out)
                shutil.rmtree(tmp, ignore_errors=True)
                return sortie
            suivant = tmp / f"niveau{i}.zip"  # archive imbriquee : copiee sur disque, pas en memoire
            with z.open(nom) as src, open(suivant, "wb") as out:
                shutil.copyfileobj(src, out)
        archive = str(suivant)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("chemin_source")
    ap.add_argument("--dest", required=True)
    a = ap.parse_args()
    print("Extrait :", extraire(a.chemin_source, a.dest))
