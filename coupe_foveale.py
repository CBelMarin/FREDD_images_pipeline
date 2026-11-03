# -*- coding: utf-8 -*-
"""
coupe_foveale.py
================

Repère, dans chaque volume OCT déjà produit par le pipeline, le B-scan qui passe
par la fovéa, et l'annote dans le DICOM (ImageComments) + un CSV récapitulatif.

POURQUOI UN MODULE À PART
-------------------------
Les volumes OCT représentent environ la moitié des B-scans livrés (de 2 à 97 coupes
par volume), alors que le périmètre FREDD ne demande que deux coupes par œil. Plutôt que de jeter les volumes, on les garde en signalant
la coupe utile. Ce module travaille sur les DICOM DÉJÀ ÉCRITS : il n'y a pas
besoin de relancer le pipeline, ni de relire les .e2e.

MÉTHODE (indépendante du constructeur, sans apprentissage)
----------------------------------------------------------
Pour chaque B-scan d'une même série (même SeriesInstanceUID), on extrait le
profil de la limitante interne (ILM) : pour chaque colonne, la première
transition franche fond -> rétine en partant du haut. Sur ce profil y(x) :

  - la dépression fovéolaire est un maximum LOCAL de y (le creux descend dans
    l'image) encadré par deux épaulements, minima locaux de y ;
  - la « proéminence » du creux = y(creux) - moyenne(y aux deux épaulements) ;
  - la coupe fovéale est celle dont la proéminence est la plus forte, à
    condition que le creux tombe dans la moitié centrale du B-scan (un volume
    maculaire est acquis centré sur la macula).

La confiance combine la proéminence absolue et l'écart avec la deuxième
meilleure coupe. Si aucune coupe ne dépasse le seuil, la série est déclarée
indéterminée et on retient la coupe médiane, en le signalant explicitement :
mieux vaut une coupe étiquetée « par défaut » qu'une fausse certitude.

LIMITES À CONNAÎTRE
-------------------
  - La méthode suppose un contour fovéolaire reconnaissable. Dans la
    rétinopathie pigmentaire, l'atteinte est d'abord périphérique et la fovéa
    reste en général conservée longtemps, ce qui est favorable ; en revanche
    dans les dystrophies maculaires (Stargardt, dystrophies des cônes) ou en cas
    d'oedème maculaire cystoïde, le creux peut être comblé, aplati ou remplacé
    par une atrophie : la détection échoue alors, et c'est ce que doit indiquer
    le statut « indéterminée » plutôt qu'une coupe choisie au hasard.
  - Les images livrées sont rééchantillonnées en 1024x496 et converties en 8
    bits : la méthode travaille sur ces images-là, pas sur les données natives.
  - Pour les séries à une ou deux coupes (lignes horizontale/verticale
    demandées par FREDD), la question ne se pose pas : la série est marquée
    « sans objet ».

ROUTE ALTERNATIVE, NON IMPLÉMENTÉE ICI
--------------------------------------
Le bloc 10004 du fichier .e2e donne, pour chaque B-scan, les coordonnées de
début et de fin du segment dans le repère du cliché infrarouge de repérage
(posX1/posY1 -> posX2/posY2, déjà lues par analyse_contenu.py), et
pipeline_wp2.detection.detect_fovea sait localiser la fovéa sur une image en
face. Croiser les deux donnerait la coupe fovéale par pure géométrie, sans
dépendre de l'aspect du creux -- donc sans la limite ci-dessus. Il manque la
correspondance entre les unités du bloc 10004 et les pixels du cliché de
repérage, à établir sur un fichier réel avec diagnostic_e2e.py. À faire si la
détection par le creux se révèle insuffisante sur les dystrophies maculaires.

USAGE
-----
    python coupe_foveale.py <dossier images_traitees>  [--csv rapport.csv]
                                                       [--pas-d-ecriture]

Sans --pas-d-ecriture, chaque DICOM de volume reçoit dans ImageComments une
mention « COUPE_FOVEALE=OUI|NON (methode, confiance) », idempotente.
"""

from __future__ import annotations

import argparse
import csv
import math
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

try:
    import pydicom
except ImportError:  # pragma: no cover
    pydicom = None

MENTION = "COUPE_FOVEALE"
#: en deçà, le creux n'est pas jugé crédible (en pixels, sur une image de 496 lignes)
SEUIL_PROEMINENCE = 8.0
#: une série de moins de 3 coupes n'est pas un volume
MIN_COUPES_VOLUME = 3


# --------------------------------------------------------------------------- #
# Profil de la limitante interne
# --------------------------------------------------------------------------- #

def profil_ilm(image: np.ndarray) -> np.ndarray:
    """Pour chaque colonne, la ligne de la première transition fond -> rétine.

    `image` : B-scan en niveaux de gris (2D). Retourne un vecteur de longueur
    égale au nombre de colonnes, en pixels, lissé. Les colonnes sans rétine
    détectée valent NaN."""
    img = image.astype(np.float32)
    if img.ndim != 2:
        raise ValueError("profil_ilm attend une image 2D")
    hauteur, largeur = img.shape

    # Lissage vertical léger : la rétine est un bloc clair, le fond est bruité.
    noyau = max(3, (hauteur // 100) * 2 + 1)
    cumul = np.cumsum(np.vstack([np.zeros((1, largeur), np.float32), img]), axis=0)
    lisse = (cumul[noyau:] - cumul[:-noyau]) / noyau  # moyenne glissante par colonne

    fond = np.percentile(img, 25)
    pic = np.percentile(img, 99)
    if pic - fond < 1e-6:
        return np.full(largeur, np.nan, np.float32)
    seuil = fond + 0.35 * (pic - fond)

    au_dessus = lisse > seuil
    # première ligne au-dessus du seuil, par colonne
    premiere = np.argmax(au_dessus, axis=0).astype(np.float32)
    premiere[~au_dessus.any(axis=0)] = np.nan
    premiere += noyau / 2.0  # recentrage de la moyenne glissante

    return _lisser(premiere, max(5, largeur // 60))


def _lisser(v: np.ndarray, fenetre: int) -> np.ndarray:
    """Médiane glissante tolérante aux NaN (supprime les colonnes aberrantes)."""
    if fenetre % 2 == 0:
        fenetre += 1
    demi = fenetre // 2
    out = np.full_like(v, np.nan)
    for i in range(len(v)):
        bloc = v[max(0, i - demi): i + demi + 1]
        bloc = bloc[~np.isnan(bloc)]
        if bloc.size:
            out[i] = float(np.median(bloc))
    return out


def proeminence_creux(profil: np.ndarray, fraction_centrale: float = 0.5):
    """Proéminence de la dépression fovéolaire et sa position.

    Retourne (proeminence_en_pixels, colonne_du_creux). La proéminence vaut 0
    si aucun creux encadré de deux épaulements n'est trouvé dans la zone
    centrale."""
    n = len(profil)
    valides = ~np.isnan(profil)
    if valides.sum() < max(20, n // 5):
        return 0.0, -1

    marge = int(n * (1 - fraction_centrale) / 2)
    zone = slice(marge, n - marge)
    sous = profil[zone]
    if np.all(np.isnan(sous)):
        return 0.0, -1

    # Le creux : point le plus BAS (y max) de la zone centrale.
    idx_local = int(np.nanargmax(sous))
    creux = marge + idx_local
    y_creux = profil[creux]

    # Épaulements : point le plus HAUT (y min) de chaque côté du creux.
    gauche = profil[:creux]
    droite = profil[creux + 1:]
    gauche = gauche[~np.isnan(gauche)]
    droite = droite[~np.isnan(droite)]
    if gauche.size < 5 or droite.size < 5:
        return 0.0, -1

    epaulement = (float(np.min(gauche)) + float(np.min(droite))) / 2.0
    return max(0.0, float(y_creux) - epaulement), creux


# --------------------------------------------------------------------------- #
# Choix de la coupe dans une série
# --------------------------------------------------------------------------- #

def choisir_coupe(profils: list[np.ndarray], seuil: float = SEUIL_PROEMINENCE):
    """profils : un profil ILM par B-scan, dans l'ordre d'acquisition.

    Retourne un dict : index retenu, méthode, proéminence, marge sur la
    deuxième meilleure coupe, et confiance ('forte', 'moyenne', 'faible',
    'indeterminee')."""
    mesures = [proeminence_creux(p) for p in profils]
    proems = [m[0] for m in mesures]
    if not proems:
        return {"index": -1, "methode": "aucune", "proeminence": 0.0, "marge": 0.0,
                "confiance": "indeterminee"}

    ordre = sorted(range(len(proems)), key=lambda i: proems[i], reverse=True)
    meilleur = ordre[0]
    second = proems[ordre[1]] if len(ordre) > 1 else 0.0
    marge = proems[meilleur] - second

    if proems[meilleur] < seuil:
        # Aucun creux crédible : on retient la coupe médiane, en le disant.
        return {"index": len(profils) // 2, "methode": "coupe_mediane_par_defaut",
                "proeminence": round(proems[meilleur], 2), "marge": round(marge, 2),
                "confiance": "indeterminee"}

    if marge >= 0.25 * proems[meilleur]:
        confiance = "forte"
    elif marge >= 0.10 * proems[meilleur]:
        confiance = "moyenne"
    else:
        confiance = "faible"
    return {"index": meilleur, "methode": "creux_fovelaire", "proeminence": round(proems[meilleur], 2),
            "marge": round(marge, 2), "confiance": confiance}


# --------------------------------------------------------------------------- #
# Parcours des DICOM
# --------------------------------------------------------------------------- #

def _gris(ds) -> np.ndarray:
    arr = ds.pixel_array
    if arr.ndim == 3:
        arr = arr[..., :3].mean(axis=-1)
    return arr.astype(np.float32)


def series_oct(dossier: Path):
    """Groupe les DICOM d'OCT par SeriesInstanceUID, triés par InstanceNumber."""
    series = defaultdict(list)
    for chemin in sorted(dossier.rglob("*.dcm")):
        try:
            ds = pydicom.dcmread(str(chemin), stop_before_pixels=True, force=True)
        except Exception:  # noqa: BLE001
            continue
        if str(getattr(ds, "Modality", "")).upper() not in ("OPT", "OCT"):
            continue
        uid = str(getattr(ds, "SeriesInstanceUID", "") or chemin.parent)
        num = getattr(ds, "InstanceNumber", None)
        series[uid].append((int(num) if num is not None else 0, chemin))
    return {uid: [c for _, c in sorted(v)] for uid, v in series.items()}


def annoter(chemin: Path, foveale: bool, infos: dict) -> bool:
    """Écrit la mention dans ImageComments. Idempotent."""
    import re
    try:
        ds = pydicom.dcmread(str(chemin), force=True)
        existant = re.sub(rf"\s*;?\s*{MENTION}=.*$", "", str(getattr(ds, "ImageComments", "") or "")).strip()
        mention = (f"{MENTION}={'OUI' if foveale else 'NON'} "
                   f"({infos['methode']}, confiance {infos['confiance']})")
        ds.ImageComments = ((existant + " ; ") if existant else "") + mention
        ds.save_as(str(chemin))
        return True
    except Exception:  # noqa: BLE001
        return False


def traiter(dossier: Path, ecrire: bool = True, chemin_csv: Path | None = None):
    if pydicom is None:
        print("[ERREUR] pydicom est requis.", file=sys.stderr)
        return 2
    groupes = series_oct(dossier)
    if not groupes:
        print(f"Aucune série OCT trouvée sous {dossier}")
        return 1

    lignes = []
    n_volumes = n_detectees = 0
    for uid, chemins in sorted(groupes.items()):
        if len(chemins) < MIN_COUPES_VOLUME:
            for c in chemins:
                lignes.append({"serie": uid, "fichier": c.name, "nb_coupes": len(chemins),
                               "coupe_foveale": "", "index": "", "methode": "sans_objet",
                               "proeminence": "", "marge": "", "confiance": "sans_objet"})
            continue
        n_volumes += 1
        profils = []
        for c in chemins:
            try:
                profils.append(profil_ilm(_gris(pydicom.dcmread(str(c), force=True))))
            except Exception:  # noqa: BLE001
                profils.append(np.full(1, np.nan, np.float32))
        res = choisir_coupe(profils)
        if res["confiance"] != "indeterminee":
            n_detectees += 1
        for i, c in enumerate(chemins):
            est = (i == res["index"])
            if ecrire:
                annoter(c, est, res)
            lignes.append({"serie": uid, "fichier": c.name, "nb_coupes": len(chemins),
                           "coupe_foveale": "OUI" if est else "NON", "index": res["index"],
                           "methode": res["methode"], "proeminence": res["proeminence"],
                           "marge": res["marge"], "confiance": res["confiance"]})

    print(f"{len(groupes)} série(s) OCT, dont {n_volumes} volume(s) de {MIN_COUPES_VOLUME} coupes ou plus.")
    if n_volumes:
        print(f"Coupe fovéale identifiée pour {n_detectees}/{n_volumes} volume(s) "
              f"({100 * n_detectees / n_volumes:.1f} %) ; les autres sont marquées « indeterminee ».")
    if chemin_csv:
        with open(chemin_csv, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(lignes[0]), delimiter=";")
            w.writeheader()
            w.writerows(lignes)
        print(f"Rapport : {chemin_csv}")
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(description="Repère la coupe OCT passant par la fovéa dans chaque volume.")
    p.add_argument("dossier", help="dossier images_traitees/ (parcouru récursivement)")
    p.add_argument("--csv", default=None, help="rapport CSV à écrire")
    p.add_argument("--pas-d-ecriture", action="store_true",
                   help="n'écrit pas dans les DICOM, produit seulement le rapport")
    a = p.parse_args(argv)
    return traiter(Path(a.dossier), ecrire=not a.pas_d_ecriture,
                   chemin_csv=Path(a.csv) if a.csv else None)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
