# -*- coding: utf-8 -*-
"""
controle_texte.py — Repère les images où du texte est probablement incrusté (sans OCR)
======================================================================================

Pourquoi : un texte incrusté dans les pixels (nom, date, numéro) n'est pas dans l'en-tête DICOM, donc
n'est pas vu par le contrôle de pseudonymisation. Ce contrôle regarde les pixels de `images_traitees/`
et signale les images qui contiennent des lignes de caractères : au moins cinq formes de la taille d'une
lettre, quasi blanches ou quasi noires, alignées sur la même ligne. Détecteur volontairement simple :
  - il produit des FAUX POSITIFS (un humain relit) et MANQUE les textes gris, peu contrastés, très gros
    ou de couleur sombre ;
  - il ne lit pas le texte, il n'en garde rien ;
  - les seuils sont des valeurs de départ, à ajuster sur quelques dizaines d'images réelles.

Ce contrôle est INTÉGRÉ au pipeline (étape 3, comme le contrôle de pseudonymisation) : une image suspecte
n'entre pas dans images_traitees/ mais dans quarantaine_pseudonymisation/ (statut ECHEC_PSEUDONYMISATION,
motif « texte probablement incrusté… »). Il est actif par défaut ; --sans-controle-texte le désactive.

La commande ci-dessous sert à RE-contrôler images_traitees/ après coup (par exemple des images traitées
avant la version 1.11.0, ou après un changement de seuils), en deux temps :
    python fredd.py texte                 mesure : totaux par centre, rien n'est déplacé
    python fredd.py texte --deplacer      déplace les images suspectes dans quarantaine_texte/
    python fredd.py texte --restaurer     remet en place celles qui ont été déplacées

Sorties : `rapports/controle_texte.csv` (suspectes seulement : fichier, centre, nombre de lignes, déplacée)
et, avec --deplacer, `quarantaine_texte/<centre>/<date>/<id_fredd>/…` (même arborescence que images_traitees/).
Une image déplacée n'est plus dans `images_traitees/` : elle n'est donc ni exportée ni cataloguée.
Après un déplacement, relancer `python fredd.py catalogue`. Ne pas relancer de traitement avant
--restaurer ou décision : l'état de reprise garde l'ancien emplacement.
"""

import csv
import shutil
import sys
from collections import Counter, defaultdict
from pathlib import Path

NOM_RAPPORT = "controle_texte.csv"
DOSSIER_QUARANTAINE = "quarantaine_texte"
ENTETE = ["fichier", "centre", "date", "lignes_suspectes", "deplacee"]
TAILLE_MAX = 1024            # image réduite à ce côté maximal avant l'analyse
HAUTEUR_CAR = (6, 40)        # hauteur d'un caractère, en pixels à cette échelle
LARGEUR_CAR_MAX = 1.6        # largeur maximale d'un caractère, en multiple de sa hauteur
REMPLISSAGE_CAR = (0.15, 0.9)  # part de pixels allumés dans la boîte du caractère
CONTRASTE_MIN = 100          # écart de niveau de gris avec le voisinage (31 px) pour un trait
CLAIR_MIN, SOMBRE_MAX = 215, 40   # un trait de texte est quasi blanc (>= 215) ou quasi noir (<= 40)
CARACTERES_MIN = 5           # nombre de caractères alignés pour parler d'une ligne de texte
SEUIL_LIGNES = 1             # nombre de lignes suspectes à partir duquel l'image est signalée


def _caracteres(gris, sens):
    """Composantes connexes de la taille d'un caractère, formées de pixels quasi blancs (sens=1)
    ou quasi noirs (sens=-1) très contrastés : [(x, y, largeur, hauteur)] triés par x."""
    import cv2
    import numpy as np
    flou = cv2.blur(gris, (31, 31)).astype(np.int16)
    extreme = (gris >= CLAIR_MIN) if sens == 1 else (gris <= SOMBRE_MAX)
    trait = (((gris.astype(np.int16) - flou) * sens > CONTRASTE_MIN) & extreme).astype(np.uint8)
    n, _, st, _ = cv2.connectedComponentsWithStats(trait, connectivity=8)
    res = []
    for i in range(1, n):
        x, y, w, h, aire = (int(v) for v in st[i])
        if (HAUTEUR_CAR[0] <= h <= HAUTEUR_CAR[1] and 1 <= w <= LARGEUR_CAR_MAX * h and aire >= 8
                and REMPLISSAGE_CAR[0] <= aire / (w * h) <= REMPLISSAGE_CAR[1]):
            res.append((x, y, w, h))
    return sorted(res)


def _lignes(caracteres):
    """Nombre de groupes d'au moins CARACTERES_MIN caractères alignés (même hauteur, même ligne, proches)."""
    m = len(caracteres)
    parent = list(range(m))

    def racine(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(m):
        xi, yi, wi, hi = caracteres[i]
        for j in range(i + 1, m):
            xj, yj, wj, hj = caracteres[j]
            ecart = xj - (xi + wi)
            if ecart > 2.5 * HAUTEUR_CAR[1]:
                break
            if (ecart >= -1 and ecart <= 2.5 * max(hi, hj) and 0.6 <= hi / hj <= 1.67
                    and abs((yi + hi / 2) - (yj + hj / 2)) <= 0.3 * max(hi, hj)):
                parent[racine(i)] = racine(j)
    return sum(1 for v in Counter(racine(i) for i in range(m)).values() if v >= CARACTERES_MIN)


def lignes_suspectes(pixels):
    """Nombre de lignes de caractères probables dans une image (tableau numpy 2D ou 3D) : du texte
    blanc ou noir, de la taille d'un caractère, aligné sur au moins CARACTERES_MIN caractères.
    Ne voit pas un texte gris, peu contraste ou très gros."""
    import cv2
    import numpy as np
    px = np.asarray(pixels)
    gris = px.mean(axis=-1) if px.ndim == 3 else px
    gris = np.clip(gris, 0, 255).astype(np.uint8)
    h, w = gris.shape
    f = TAILLE_MAX / max(h, w)
    if f < 1:
        gris = cv2.resize(gris, (max(1, int(w * f)), max(1, int(h * f))), interpolation=cv2.INTER_AREA)
    if min(gris.shape) < 2 * HAUTEUR_CAR[0]:
        return 0
    return sum(_lignes(_caracteres(gris, sens)) for sens in (1, -1))


def _pixels(path):
    import pydicom
    ds = pydicom.dcmread(str(path), force=True)
    px = ds.pixel_array
    if px.ndim == 4 or (px.ndim == 3 and int(getattr(ds, "SamplesPerPixel", 1) or 1) == 1):
        px = px[0]
    return px


def controler_sortie(path, seuil=SEUIL_LIGNES):
    """Contrôle intégré au pipeline, pour un DICOM produit : (True, "") si rien de suspect ;
    (False, motif) si du texte est probable OU si l'image n'a pas pu être analysée (par prudence)."""
    try:
        n = lignes_suspectes(_pixels(path))
    except Exception as e:  # noqa: BLE001
        return False, f"contrôle du texte incrusté impossible ({type(e).__name__})"
    if n >= seuil:
        return False, f"texte probablement incrusté dans les pixels ({n} ligne(s) de caractères suspectées)"
    return True, ""


def analyser(sortie, seuil=SEUIL_LIGNES, log=print):
    """[(chemin, centre, date, nb_lignes)] des images suspectes ; retourne aussi les totaux par centre."""
    racine = Path(sortie) / "images_traitees"
    suspectes, totaux, illisibles = [], Counter(), 0
    for f in sorted(racine.rglob("*.dcm")):
        rel = f.relative_to(racine).parts
        if len(rel) < 3:
            continue
        centre, date = rel[0], rel[1]
        totaux[centre] += 1
        try:
            n = lignes_suspectes(_pixels(f))
        except Exception:  # noqa: BLE001
            illisibles += 1
            continue
        if n >= seuil:
            suspectes.append((f, centre, date, n))
    return suspectes, totaux, illisibles


def deplacer(sortie, suspectes):
    racine, quarantaine = Path(sortie) / "images_traitees", Path(sortie) / DOSSIER_QUARANTAINE
    for f, *_ in suspectes:
        dest = quarantaine / f.relative_to(racine)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(f), str(dest))


def ecrire_rapport(sortie, suspectes, deplacees):
    chemin = Path(sortie) / "rapports" / NOM_RAPPORT
    chemin.parent.mkdir(parents=True, exist_ok=True)
    racine = Path(sortie) / "images_traitees"
    with open(chemin, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh, delimiter=";")
        w.writerow(ENTETE)
        for f, centre, date, n in suspectes:
            w.writerow([f.relative_to(racine).as_posix(), centre, date, n, "oui" if deplacees else "non"])
    return chemin


def restaurer(sortie, log=print):
    quarantaine, racine = Path(sortie) / DOSSIER_QUARANTAINE, Path(sortie) / "images_traitees"
    n = 0
    for f in sorted(quarantaine.rglob("*.dcm")) if quarantaine.is_dir() else []:
        dest = racine / f.relative_to(quarantaine)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(f), str(dest))
        n += 1
    log(f"{n} image(s) remise(s) dans images_traitees/. Relancer « catalogue » pour mettre à jour les fiches.")
    return 0


def afficher(suspectes, totaux, illisibles, deplacees, log=print):
    par = defaultdict(int)
    for _, centre, *_ in suspectes:
        par[centre] += 1
    log("Contrôle du texte incrusté (détecteur simple, à relire) : centre ; images ; suspectes")
    for centre in sorted(totaux):
        log(f"  {centre} ; {totaux[centre]} ; {par[centre]}")
    log(f"  TOTAL ; {sum(totaux.values())} ; {len(suspectes)}" + (f" ; illisibles : {illisibles}" if illisibles else ""))
    log("Suspectes " + ("déplacées dans quarantaine_texte/." if deplacees else
                        "non déplacées (option --deplacer pour les mettre à l'écart)."))


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if not argv or argv[0].startswith("--"):
        print("Usage : python controle_texte.py <dossier de sortie> [--deplacer | --restaurer]   (ou : python fredd.py texte)")
        return 2
    sortie, options = argv[0], set(argv[1:])
    if not (Path(sortie) / "images_traitees").is_dir():
        print(f"[ERREUR] {Path(sortie) / 'images_traitees'} introuvable.")
        return 2
    if "--restaurer" in options:
        return restaurer(sortie)
    suspectes, totaux, illisibles = analyser(sortie)
    a_deplacer = "--deplacer" in options
    if a_deplacer:
        deplacer(sortie, suspectes)
    ecrire_rapport(sortie, suspectes, a_deplacer)
    afficher(suspectes, totaux, illisibles, a_deplacer)
    return 0


if __name__ == "__main__":
    sys.exit(main())
