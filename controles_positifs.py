# -*- coding: utf-8 -*-
"""
controles_positifs.py — Mesure la SENSIBILITÉ du détecteur de texte incrusté
============================================================================

POURQUOI
--------
Le contrôle visuel d'un échantillon d'images livrées ne peut que **borner** le
taux de texte résiduel : s'il ne trouve rien, il ne dit rien de ce que le
détecteur laisse passer. Un échantillon sans positif ne permet aucune mesure de
sensibilité. Pour chiffrer ce que `controle_texte.py` rate, il faut des positifs
connus : on en fabrique, en réincrustant des bandeaux d'identification dans des
images DÉJÀ traitées, puis on compte combien le détecteur en retrouve.

Ce script ne remplace pas le contrôle visuel : les deux sont complémentaires.
Le contrôle visuel borne le taux résiduel réel, celui-ci mesure la sensibilité
par condition (contraste, taille, position), donc dit **où** le détecteur est
aveugle et quels seuils ajuster.

CE QUI EST FABRIQUÉ
-------------------
Des bandeaux imitant ceux des exports constructeurs : un nom, une date de
naissance, un identifiant, une date d'examen. Les valeurs sont **fictives et
manifestement fausses** (DUPONT JEAN, 01/01/1950, ID 000000) : aucune donnée
réelle n'est écrite nulle part. Les images fabriquées sont écrites dans un
dossier dédié, JAMAIS dans `images_traitees/`, et sont à supprimer après la
mesure.

Les conditions croisées :
  - contraste : blanc (255), gris clair (190), gris moyen (128), gris foncé
    (70), noir (0) — le détecteur ne retient par construction que >= 215 ou
    <= 40, donc les trois valeurs intermédiaires mesurent ce qu'il rate ;
  - taille de police : petite, moyenne, grande ;
  - position : bande haute, bande basse, coin supérieur gauche ;
  - fond : marge sombre de l'image, ou en plein sur la rétine.

USAGE
-----
    python controles_positifs.py <dossier images_traitees> --graine 2026
                                 [--images 40] [--sortie controles_positifs]
                                 [--garder-images]

Sorties : `controles_positifs.csv` (une ligne par image fabriquée) et un
tableau de sensibilité par condition affiché à l'écran.
"""

from __future__ import annotations

import argparse
import csv
import random
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

import controle_texte as ct

#: valeurs fictives, jamais des données réelles
LIGNES_BANDEAU = ["DUPONT JEAN", "01/01/1950", "ID 000000", "EXAM 02/02/2026"]

NIVEAUX = {"blanc": 255, "gris_clair": 190, "gris_moyen": 128, "gris_fonce": 70, "noir": 0}
TAILLES = {"petite": 0.45, "moyenne": 0.75, "grande": 1.20}
POSITIONS = ("bande_haute", "bande_basse", "coin_haut_gauche")
FONDS = ("marge_sombre", "sur_retine")


def _incruster(image: np.ndarray, niveau: int, echelle: float, position: str, fond: str) -> np.ndarray:
    """Réincruste un bandeau d'identification fictif. Retourne une copie."""
    import cv2

    img = image.copy()
    if img.ndim == 3:
        gris_vers_couleur = True
        h, w = img.shape[:2]
    else:
        gris_vers_couleur = False
        h, w = img.shape

    police = cv2.FONT_HERSHEY_SIMPLEX
    epaisseur = max(1, int(round(echelle * 2)))
    (_, hauteur_texte), _ = cv2.getTextSize("Ag", police, echelle, epaisseur)
    interligne = int(hauteur_texte * 1.8)
    marge = int(0.02 * h)

    if position == "bande_haute":
        x0, y0 = marge, marge + hauteur_texte
    elif position == "bande_basse":
        x0, y0 = marge, h - marge - interligne * (len(LIGNES_BANDEAU) - 1)
    else:  # coin_haut_gauche
        x0, y0 = marge, marge + hauteur_texte

    if fond == "sur_retine":
        # on décale au centre, là où l'image est claire et texturée
        x0 = max(marge, w // 2 - int(0.2 * w))
        y0 = max(marge + hauteur_texte, h // 2 - interligne)

    couleur = (niveau, niveau, niveau) if gris_vers_couleur else niveau
    for i, texte in enumerate(LIGNES_BANDEAU):
        cv2.putText(img, texte, (x0, y0 + i * interligne), police, echelle, couleur, epaisseur, cv2.LINE_AA)
    return img


def _images_sources(dossier: Path, n: int, graine: int):
    """Tire n DICOM au hasard, de façon reproductible."""
    tous = sorted(dossier.rglob("*.dcm"))
    if not tous:
        return []
    rng = random.Random(f"controles_positifs|{graine}")
    return rng.sample(tous, min(n, len(tous)))


def mesurer(dossier: Path, n_images: int, graine: int, sortie: Path, garder: bool):
    import pydicom

    sources = _images_sources(dossier, n_images, graine)
    if not sources:
        print(f"Aucun DICOM sous {dossier}", file=sys.stderr)
        return 1
    sortie.mkdir(parents=True, exist_ok=True)

    lignes = []
    print(f"{len(sources)} image(s) source, "
          f"{len(NIVEAUX) * len(TAILLES) * len(POSITIONS) * len(FONDS)} condition(s) par image.")
    for chemin in sources:
        try:
            px = ct._pixels(chemin)
        except Exception as e:  # noqa: BLE001
            print(f"  [ignorée] {chemin.name} ({type(e).__name__})")
            continue
        base = np.asarray(px)
        # témoin négatif : l'image telle quelle doit rester non suspecte
        temoin = ct.lignes_suspectes(base)
        lignes.append({"fichier": chemin.name, "condition": "temoin_sans_texte", "niveau": "",
                       "taille": "", "position": "", "fond": "", "lignes_detectees": temoin,
                       "detecte": "oui" if temoin >= ct.SEUIL_LIGNES else "non"})
        for nom_niveau, niveau in NIVEAUX.items():
            for nom_taille, echelle in TAILLES.items():
                for position in POSITIONS:
                    for fond in FONDS:
                        img = _incruster(base, niveau, echelle, position, fond)
                        n = ct.lignes_suspectes(img)
                        lignes.append({"fichier": chemin.name, "condition": "positif",
                                       "niveau": nom_niveau, "taille": nom_taille,
                                       "position": position, "fond": fond,
                                       "lignes_detectees": n,
                                       "detecte": "oui" if n >= ct.SEUIL_LIGNES else "non"})
                        if garder:
                            cible = sortie / "images" / f"{chemin.stem}_{nom_niveau}_{nom_taille}_{position}_{fond}.npy"
                            cible.parent.mkdir(parents=True, exist_ok=True)
                            np.save(cible, img)

    chemin_csv = sortie / "controles_positifs.csv"
    with open(chemin_csv, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(lignes[0]), delimiter=";")
        w.writeheader()
        w.writerows(lignes)

    _afficher(lignes, graine)
    print(f"\nDétail : {chemin_csv}")
    if garder:
        print(f"Images fabriquées conservées dans {sortie / 'images'} — À SUPPRIMER après lecture.")
    return 0


def _afficher(lignes, graine):
    positifs = [l for l in lignes if l["condition"] == "positif"]
    temoins = [l for l in lignes if l["condition"] == "temoin_sans_texte"]
    faux_positifs = sum(1 for l in temoins if l["detecte"] == "oui")

    def taux(sous):
        return (100 * sum(1 for l in sous if l["detecte"] == "oui") / len(sous)) if sous else float("nan")

    print(f"\nSensibilité globale (graine {graine}) : {taux(positifs):.1f} % "
          f"sur {len(positifs)} image(s) fabriquée(s)")
    print(f"Témoins sans texte signalés à tort : {faux_positifs}/{len(temoins)}")
    for cle, libelle in (("niveau", "contraste"), ("taille", "taille de police"),
                         ("position", "position"), ("fond", "fond")):
        print(f"\n  par {libelle} :")
        groupes = defaultdict(list)
        for l in positifs:
            groupes[l[cle]].append(l)
        for k in sorted(groupes, key=lambda k: -taux(groupes[k])):
            print(f"    {k:18} {taux(groupes[k]):5.1f} %   (n={len(groupes[k])})")
    print("\nLecture : le détecteur ne retient par construction que les traits >= 215 ou <= 40 "
          "(CLAIR_MIN / SOMBRE_MAX de controle_texte.py). Les lignes « gris_* » mesurent donc "
          "ce qu'il laisse passer, et non un défaut de réglage à corriger à l'aveugle.")


def main(argv=None):
    p = argparse.ArgumentParser(description="Mesure la sensibilité du détecteur de texte incrusté "
                                            "sur des positifs fabriqués.")
    p.add_argument("dossier", help="dossier images_traitees/")
    p.add_argument("--graine", type=int, required=True, help="graine du tirage (à noter dans le papier)")
    p.add_argument("--images", type=int, default=40, help="nombre d'images sources (défaut : 40)")
    p.add_argument("--sortie", default="controles_positifs", help="dossier de sortie")
    p.add_argument("--garder-images", action="store_true",
                   help="conserve les images fabriquées (à supprimer ensuite)")
    a = p.parse_args(argv)
    return mesurer(Path(a.dossier), a.images, a.graine, Path(a.sortie), a.garder_images)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
