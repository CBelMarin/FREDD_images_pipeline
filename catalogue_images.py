# -*- coding: utf-8 -*-
"""
catalogue_images.py — Une fiche par image traitée, à transmettre avec les images
=================================================================================

Écrit `catalogue_images.csv` dans chaque dossier `images_traitees/<centre>/<date>/`, avec une ligne
par DICOM : centre, id_fredd, fichier, type, latéralité, orientation, taille, part de pixels utiles,
verdict de qualité technique, score, problèmes.

Le catalogue est lu dans les DICOM eux-mêmes (PatientID, ImageLaterality, ImageComments…), donc :
  - il ne contient ni chemin source ni nom d'origine : il peut partir avec les images ;
  - il se reconstruit à tout moment, sans retraiter ;
  - les images traitées avant la version 1.11.0 (sans mention de qualité) sont marquées NON_EVALUEE.
Les images de qualité limitée sont en tête du fichier. L'export (export_images.py) l'ajoute à chaque nouveau lot.

    python fredd.py catalogue
"""

import csv
import os
import re
import sys
from collections import Counter
from pathlib import Path

NOM = "catalogue_images.csv"
ENTETE = ["centre", "id_fredd", "fichier", "image_type", "lateralite", "orientation_coupe", "champ", "mode_uwf", "largeur_px",
          "hauteur_px", "pct_pixels_utiles", "qualite", "score_qualite", "problemes", "date_traitement"]
ORDRE_QUALITE = {"LIMITEE": 0, "INCONNU": 1, "NON_EVALUEE": 2, "OK": 3}


def _mesures(path, pct_connu):
    import pipeline_fredd as pf
    if pct_connu not in (None, ""):
        return pct_connu
    return pf.mesures_image_dicom(path)[2]


def fiche(path, lot, pct_connu=None):
    """Ligne du catalogue pour un DICOM de `lot` (dossier images_traitees/<centre>/<date>)."""
    import pydicom
    ds = pydicom.dcmread(str(path), stop_before_pixels=True, force=True)
    commentaire = str(getattr(ds, "ImageComments", "") or "")
    m = re.search(r"orientation\s*:\s*([^;]+)", commentaire)
    ch = re.search(r"champ\s*:\s*([^;(]+)", commentaire)
    u = re.search(r"uwf\s*:\s*(\w+)", commentaire)
    q = re.search(r"QUALITE_TECHNIQUE=(\w+)", commentaire)
    s = re.search(r"score=([\d.]+)", commentaire)
    p = re.search(r"problemes=(.*)$", commentaire)
    type_ = str(getattr(ds, "SeriesDescription", "") or "").replace("Processed ", "", 1)
    return {
        "centre": lot.parent.name, "id_fredd": str(getattr(ds, "PatientID", "") or ""),
        "fichier": path.relative_to(lot).as_posix(), "image_type": type_,
        "lateralite": str(getattr(ds, "ImageLaterality", "") or ""),
        "orientation_coupe": m.group(1).strip() if m else "",
        "champ": ch.group(1).strip() if ch else "",
        "mode_uwf": u.group(1) if u else "",   # « natif » (capture étiquetée UWF) | « montage » ; "" sinon
        "largeur_px": int(getattr(ds, "Columns", 0) or 0), "hauteur_px": int(getattr(ds, "Rows", 0) or 0),
        "pct_pixels_utiles": _mesures(path, pct_connu),
        "qualite": ("LIMITEE" if q.group(1) == "A_VERIFIER" else q.group(1)) if q else "NON_EVALUEE",  # A_VERIFIER = ancien libellé (<= 1.12.1)
        "score_qualite": s.group(1) if s else "",
        "problemes": p.group(1).strip() if p else "", "date_traitement": lot.name,
    }


def _pct_connus(sortie):
    """{fichier_sortie (relatif à sortie, séparateur /): pct_pixels_utiles} d'après l'état de reprise."""
    res = {}
    chemin = Path(sortie) / "rapports" / "etat_sorties.csv"
    if chemin.is_file():
        with open(chemin, encoding="utf-8-sig", newline="") as f:
            for l in csv.DictReader(f, delimiter=";"):
                if l.get("fichier_sortie") and l.get("pct_pixels_utiles"):
                    res[l["fichier_sortie"].replace("\\", "/")] = l["pct_pixels_utiles"]
    return res


def construire(sortie, log=print):
    """Réécrit le catalogue de chaque lot. Retourne {(centre, date): Counter(qualite)}."""
    racine = Path(sortie) / "images_traitees"
    if not racine.is_dir():
        log(f"[ERREUR] {racine} introuvable.")
        return None
    connus = _pct_connus(sortie)
    bilan = {}
    for centre in sorted(p for p in racine.iterdir() if p.is_dir()):
        for lot in sorted(p for p in centre.iterdir() if p.is_dir()):
            lignes = []
            for f in sorted(lot.rglob("*.dcm")):
                cle = f.relative_to(Path(sortie)).as_posix()
                try:
                    lignes.append(fiche(f, lot, connus.get(cle)))
                except Exception as e:  # noqa: BLE001
                    log(f"[ATTENTION] {centre.name}/{lot.name} : un fichier illisible ({type(e).__name__})")
            if not lignes:
                continue
            lignes.sort(key=lambda l: (ORDRE_QUALITE.get(l["qualite"], 1), l["id_fredd"], l["fichier"]))
            tmp = lot / (NOM + ".partiel")
            with open(tmp, "w", encoding="utf-8-sig", newline="") as f:
                w = csv.DictWriter(f, fieldnames=ENTETE, delimiter=";")
                w.writeheader()
                w.writerows(lignes)
            os.replace(tmp, lot / NOM)
            bilan[(centre.name, lot.name)] = Counter(l["qualite"] for l in lignes)
    return bilan


def afficher(bilan, log=print):
    if not bilan:
        log("Aucune image traitée : pas de catalogue.")
        return
    total = Counter()
    for c in bilan.values():
        total.update(c)
    log(f"Catalogue des images : {sum(total.values())} image(s) dans {len(bilan)} lot(s) ; "
        f"qualité limitée : {total['LIMITEE']} ; non évaluées (anciennes) : {total['NON_EVALUEE']}.")


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        print("Usage : python catalogue_images.py <dossier de sortie>   (ou : python fredd.py catalogue)")
        return 2
    bilan = construire(argv[0])
    if bilan is None:
        return 2
    afficher(bilan)
    return 0


if __name__ == "__main__":
    sys.exit(main())
