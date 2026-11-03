# -*- coding: utf-8 -*-
"""
analyse_patients.py — Combien d'images exploitables par patient ? (totaux uniquement)
====================================================================================

Lit rapports/journal_images.csv. Une image est « exploitable » si elle est rattachée à un patient
FREDD et destinée au traitement (ou déjà traitée) : ni doublon, ni Raw Data, ni infrarouge, ni mise
à l'écart, ni format non pris en charge.

Par centre : nombre de patients avec au moins une image exploitable, total d'images, moyenne,
médiane, 10e et 90e centiles, maximum. Aucun identifiant ni chemin dans la sortie.

    python fredd.py patients
"""

import csv
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path

STATUTS_EXPLOITABLES = {"A_TRAITER", "A_TRAITER_SIMULATION", "HORS_ECHANTILLON",
                        "TRAITEE_OK", "TRAITEE_QUALITE_LIMITEE", "TRAITEE_QUALITE_INCONNUE"}
COLONNES = ["patients", "images", "moyenne", "mediane", "p10", "p90", "max"]


def _centile(valeurs, q):
    ordonnees = sorted(valeurs)
    return ordonnees[min(len(ordonnees) - 1, int(q * len(ordonnees)))]


def analyser(journal):
    """{centre: [nombre d'images exploitables par patient, ...]}."""
    par_centre = defaultdict(Counter)
    with open(journal, encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f, delimiter=";"):
            pid = (r.get("id_fredd") or "").strip()
            if pid and r.get("statut") in STATUTS_EXPLOITABLES:
                par_centre[(r.get("centre") or "").strip() or "(centre non renseigné)"][pid] += 1
    return {c: list(cpt.values()) for c, cpt in par_centre.items()}


def resume(valeurs):
    return {"patients": len(valeurs), "images": sum(valeurs),
            "moyenne": f"{statistics.mean(valeurs):.1f}", "mediane": f"{statistics.median(valeurs):g}",
            "p10": _centile(valeurs, 0.10), "p90": _centile(valeurs, 0.90), "max": max(valeurs)}


def afficher(par_centre, log=print):
    log("Images exploitables par patient (totaux uniquement)")
    log("; ".join(["centre"] + COLONNES))
    tout = []
    for centre in sorted(par_centre):
        tout += par_centre[centre]
        res = resume(par_centre[centre])
        log("; ".join([centre] + [str(res[k]) for k in COLONNES]))
    if tout:
        res = resume(tout)
        log("; ".join(["TOUS"] + [str(res[k]) for k in COLONNES]))


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        print("Usage : python analyse_patients.py <dossier de sortie>   (ou : python fredd.py patients)")
        return 2
    journal = Path(argv[0]) / "rapports" / "journal_images.csv"
    if not journal.is_file():
        print(f"[ERREUR] {journal} introuvable.")
        return 2
    afficher(analyser(journal))
    return 0


if __name__ == "__main__":
    sys.exit(main())
