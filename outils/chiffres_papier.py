# -*- coding: utf-8 -*-
"""
chiffres_papier.py : regenere TOUS les chiffres cites dans le manuscrit a partir du dossier rapports/ d'un passage.
Aucune ligne de sortie ne contient de chemin ni d'identifiant : uniquement des effectifs.

Usage :  python chiffres_papier.py <dossier_rapports> [--sortie chiffres_papier.md]
"""
import argparse
import csv
import json
import sys
from collections import Counter
from pathlib import Path

csv.field_size_limit(2 ** 31 - 1)


def lire(p):
    with open(p, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f, delimiter=";"))


def pct(a, b):
    return f"{100 * a / b:.1f} %" if b else "n/d"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("rapports")
    ap.add_argument("--sortie", default="chiffres_papier.md")
    a = ap.parse_args()
    R = Path(a.rapports)
    ex = json.loads((R / "execution.json").read_text(encoding="utf-8-sig"))
    st = Counter(ex["statuts"])
    L = []

    def t(titre):
        L.append(f"\n## {titre}\n")

    def l(nom, val):
        L.append(f"- {nom} : **{val}**")

    L.append("# Chiffres du manuscrit (genere automatiquement)")
    l("Version du pipeline", ex.get("version_pipeline"))
    l("Version du module pixel", ex.get("raretia", {}).get("version"))
    duree_h = float(str(ex.get("duree", "0")).split()[0]) / 3600
    l("Duree du passage (h)", f"{duree_h:.1f}")
    l("Debut / fin", f"{ex.get('debut')} / {ex.get('fin')}")

    t("Fichiers sources et doublons")
    total = sum(st.values())
    dbl = st.get("IGNOREE_DOUBLON", 0)
    l("Fichiers inventories", f"{total:,}")
    l("Doublons ignores (meme SHA-256 et meme patient)", f"{dbl:,} ({pct(dbl, total)})")
    l("Fichiers uniques", f"{total - dbl:,}")
    for k, v in sorted(st.items(), key=lambda x: -x[1]):
        suite = "" if k == "IGNOREE_DOUBLON" else f" ; {pct(v, total - dbl)} des uniques"
        l(f"Statut {k}", f"{v:,} ({pct(v, total)} des fichiers{suite})")

    t("Sorties DICOM")
    S = lire(R / "sorties_traitees.csv")
    l("Sorties DICOM (lignes du catalogue)", f"{len(S):,} (execution.json : {ex.get('nb_dicom_produits')})")
    rang = Counter(r["rangement"] for r in S)
    for k, v in rang.most_common():
        l(f"Rangement « {k} »", f"{v:,}")
    l("Quarantaine (execution.json)", ex.get("nb_quarantaine"))
    for k, v in Counter(r["image_type"] for r in S).most_common():
        l(f"Type {k}", f"{v:,}")
    acc = Counter(r["acceptee"] for r in S)
    l("Images acceptees / non acceptees", f"{acc.get('OUI', 0):,} / {len(S) - acc.get('OUI', 0):,}")
    l("Pseudonymisation non « OK » (colonne pseudonymisation non vide)", sum(1 for r in S if r.get("pseudonymisation") not in ("", "OK")))

    t("Dimensions (modalite OCT)")
    oct_ = [r for r in S if r["image_type"] == "oct_bscan"]
    dims = Counter((r["largeur_px"], r["hauteur_px"]) for r in oct_)
    for (w, h), n in dims.most_common(6):
        l(f"OCT {w} x {h}", f"{n:,} ({pct(n, len(oct_))})")
    for k, n in Counter((r["image_type"], r["largeur_px"], r["hauteur_px"]) for r in S if r["image_type"] != "oct_bscan").most_common(6):
        l(f"{k[0]} {k[1]} x {k[2]}", f"{n:,}")

    t("Qualite")
    sc = [float(r["score_qualite"]) for r in S if r["score_qualite"] not in ("", None)]
    if sc:
        sc.sort()
        l("Score qualite median (n=%d)" % len(sc), f"{sc[len(sc) // 2]:.3f}")
    pb = Counter()
    for r in S:
        for p in (r.get("problemes") or "").split(";"):
            p = p.strip()
            if p:
                pb[p] += 1
    for k, v in pb.most_common(8):
        l(f"Probleme « {k} »", f"{v:,}")

    t("Patients et confrontation SKEZIA")
    for k, v in ex.get("confrontation", {}).items():
        l(k, f"{v:,}")
    for r in lire(R / "synthese_execution.csv"):
        if r["indicateur"].startswith(("Nombre de patients", "… dont", "Images par")):
            l(r["indicateur"], r["valeur"])

    t("Par centre")
    for r in lire(R / "bilan_par_centre.csv"):
        l(f"Centre {r['centre']}", f"{r['nb_images_inventoriees']} fichiers, {r['nb_patients_fredd']} patients, {r['nb_sorties_dicom']} sorties")

    Path(a.sortie).write_text("\n".join(L) + "\n", encoding="utf-8")
    print("\n".join(L))
    print(f"\n-> {a.sortie}")


if __name__ == "__main__":
    sys.exit(main())
