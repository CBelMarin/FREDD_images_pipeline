# -*- coding: utf-8 -*-
"""
analyse_doublons.py — D'où viennent les doublons ? (totaux uniquement)
=====================================================================

Lit rapports/journal_images.csv et classe chaque doublon (statut IGNOREE_DOUBLON) selon la
position de la copie ignorée par rapport à la copie gardée :

  - meme_dossier        : les deux copies sont dans le même dossier ;
  - meme_transfert      : dossiers différents, sous le même dossier de 1er niveau (même transfert) ;
  - transferts_differents : dossiers de 1er niveau différents (ex. deux transferts successifs) ;
  - autre_racine        : la copie gardée est hors du dossier scanné de la copie ignorée.

Une colonne indique aussi combien de doublons impliquent un zip (chemin contenant « .zip :: »).

La sortie ne contient QUE des totaux par centre : aucun nom de fichier, de dossier ni
d'identifiant patient. Elle peut donc être recopiée à la main hors de l'environnement sécurisé.

    python fredd.py doublons
"""

import csv
import os
import sys
from collections import Counter, defaultdict
from pathlib import Path

SEP_ZIP = " :: "
PREFIXE_MOTIF = "Identique à : "
CLASSES = ["meme_dossier", "meme_transfert", "transferts_differents", "autre_racine"]


def _morceaux(chemin):
    return chemin.split(SEP_ZIP)


def _dossier(chemin):
    """Dossier contenant le fichier ; pour un fichier dans un zip : le zip + le dossier interne."""
    m = _morceaux(chemin)
    if len(m) == 1:
        return os.path.normcase(os.path.dirname(m[0]))
    interne = os.path.dirname(m[-1].replace("\\", "/"))
    return os.path.normcase("|".join([m[0]] + m[1:-1] + [interne]))


def _premier_niveau(chemin, racine):
    """Premier composant du chemin relatif à la racine scannée, ou None si hors racine."""
    disque = _morceaux(chemin)[0]
    try:
        rel = os.path.relpath(disque, racine)
    except ValueError:
        return None
    parts = [p for p in rel.replace("\\", "/").split("/") if p not in ("", ".")]
    if not parts or parts[0] == "..":
        return None
    return os.path.normcase(parts[0])


def classer(copie_ignoree, copie_gardee, racine):
    """Retourne (classe, implique_un_zip)."""
    zip_ = SEP_ZIP in copie_ignoree or SEP_ZIP in copie_gardee
    if _dossier(copie_ignoree) == _dossier(copie_gardee):
        return "meme_dossier", zip_
    n1, n2 = _premier_niveau(copie_ignoree, racine), _premier_niveau(copie_gardee, racine)
    if n1 is None or n2 is None:
        return "autre_racine", zip_
    return ("meme_transfert" if n1 == n2 else "transferts_differents"), zip_


def analyser(journal):
    """{centre: Counter} d'après un journal_images.csv ; la clé 'total' compte les doublons,
    'zip' ceux qui impliquent un zip, 'sans_copie_gardee' ceux dont le motif est illisible."""
    res = defaultdict(Counter)
    totaux_images = Counter()
    with open(journal, encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f, delimiter=";"):
            centre = (r.get("centre") or "").strip() or "(centre non renseigné)"
            totaux_images[centre] += 1
            if r.get("statut") != "IGNOREE_DOUBLON":
                continue
            c = res[centre]
            c["total"] += 1
            motif = r.get("motif") or ""
            if not motif.startswith(PREFIXE_MOTIF):
                c["sans_copie_gardee"] += 1
                continue
            classe, zip_ = classer(r.get("chemin_source") or "", motif[len(PREFIXE_MOTIF):],
                                   r.get("racine_scan") or "")
            c[classe] += 1
            c["zip"] += zip_
    for centre, n in totaux_images.items():
        res[centre]["images"] = n
    return res


def afficher(res, log=print):
    colonnes = ["images", "total"] + CLASSES + ["zip", "sans_copie_gardee"]
    log("Doublons par centre (totaux uniquement)")
    log("; ".join(["centre"] + colonnes))
    somme = Counter()
    for centre in sorted(res):
        ligne = [str(res[centre][k]) for k in colonnes]
        somme.update({k: res[centre][k] for k in colonnes})
        log("; ".join([centre] + ligne))
    log("; ".join(["TOTAL"] + [str(somme[k]) for k in colonnes]))


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        print("Usage : python analyse_doublons.py <dossier de sortie>   (ou : python fredd.py doublons)")
        return 2
    journal = Path(argv[0]) / "rapports" / "journal_images.csv"
    if not journal.is_file():
        print(f"[ERREUR] {journal} introuvable.")
        return 2
    afficher(analyser(journal))
    return 0


if __name__ == "__main__":
    sys.exit(main())
