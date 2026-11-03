# -*- coding: utf-8 -*-
"""
comparer_passages.py : compare deux passages du pipeline FREDD, par COMPTAGES uniquement.

Aucun chemin, aucun nom, aucun identifiant n'est affiche ni ecrit : seuls des effectifs.

Usage (dans l'environnement sécurisé) :
    python comparer_passages.py --ancien <dossier_sortie_ancien> --nouveau <dossier_sortie_nouveau>

Lit <sortie>\\rapports\\journal_images.csv et synthese_execution.csv de chaque passage,
affiche le resultat a l'ecran et l'ecrit dans comparaison_passages.txt (dossier courant).
"""

import argparse
import csv
import sys
from collections import Counter, defaultdict
from pathlib import Path

try:
    sys.stdout.reconfigure(errors="replace")  # console Windows : evite les erreurs d'encodage
except Exception:
    pass
csv.field_size_limit(2**31 - 1)  # sys.maxsize deborde sous Windows (long C sur 32 bits)


def lire(chemin):
    with open(chemin, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f, delimiter=";"))


def journal(sortie):
    p = Path(sortie) / "rapports" / "journal_images.csv"
    if not p.exists():
        sys.exit(f"Introuvable : {p}")
    return lire(p)


def synthese(sortie):
    p = Path(sortie) / "rapports" / "synthese_execution.csv"
    return {r["indicateur"]: r["valeur"] for r in lire(p)} if p.exists() else {}


def modalites(contenu):
    """Resume le contenu detecte en modalites (premier mot de chaque element), sans lateralite."""
    mots = sorted({e.strip().split(" ")[0] for e in (contenu or "").split(";") if e.strip()})
    return "+".join(mots) if mots else "inconnu"


def tableau(titre, compteur, sortie, largeur=44):
    sortie.append("")
    sortie.append(titre)
    if not compteur:
        sortie.append("  (rien)")
        return
    for cle, n in sorted(compteur.items(), key=lambda x: (-x[1], str(x[0]))):
        lib = " | ".join(str(c) for c in cle) if isinstance(cle, tuple) else str(cle)
        sortie.append(f"  {n:7d}  {lib}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ancien", required=True)
    ap.add_argument("--nouveau", required=True)
    a = ap.parse_args()

    anc, nou = journal(a.ancien), journal(a.nouveau)
    out = [f"COMPARAISON DE DEUX PASSAGES (effectifs seulement)",
           f"Ancien : {len(anc)} lignes. Nouveau : {len(nou)} lignes."]

    for nom, j in (("ancien", anc), ("nouveau", nou)):
        tableau(f"Fichiers par centre, passage {nom}", Counter(r["centre"] for r in j), out)

    # Cle de rapprochement : chemin source ; repli sur l'empreinte si les racines different.
    cle_anc = {r["chemin_source"]: r for r in anc}
    cle_nou = {r["chemin_source"]: r for r in nou}
    communs = set(cle_anc) & set(cle_nou)
    methode = "chemin source"
    if len(communs) < 0.5 * min(len(cle_anc), len(cle_nou)):
        # l'empreinte n'est pas unique (doublons) : on garde un exemplaire par (empreinte, centre)
        cle_anc = {(r["sha256"], r["id_fredd"] or r["patient_id_detecte"]): r for r in anc if r.get("sha256")}
        cle_nou = {(r["sha256"], r["id_fredd"] or r["patient_id_detecte"]): r for r in nou if r.get("sha256")}
        communs = set(cle_anc) & set(cle_nou)
        methode = "empreinte SHA-256 + patient (les chemins ne correspondent pas)"
    out.append("")
    out.append(f"Rapprochement par : {methode}")
    out.append(f"Communs : {len(communs)} | seulement dans l'ancien : {len(set(cle_anc) - communs)} | "
               f"seulement dans le nouveau : {len(set(cle_nou) - communs)}")

    seul_anc = [cle_anc[k] for k in set(cle_anc) - communs]
    seul_nou = [cle_nou[k] for k in set(cle_nou) - communs]
    tableau("Seulement dans l'ANCIEN : centre | statut", Counter((r["centre"], r["statut"]) for r in seul_anc), out)
    tableau("Seulement dans le NOUVEAU : centre | statut | categorie | contenu",
            Counter((r["centre"], r["statut"], r.get("categorie", ""), modalites(r.get("contenu_detecte", "")))
                    for r in seul_nou), out)

    trans = Counter()
    for k in communs:
        so, sn = cle_anc[k]["statut"], cle_nou[k]["statut"]
        trans[(so, sn)] += 1
    ident = sum(n for (so, sn), n in trans.items() if so == sn)
    out.append("")
    out.append(f"Fichiers communs au statut identique : {ident} sur {len(communs)}")
    tableau("Fichiers communs dont le statut CHANGE : ancien -> nouveau",
            Counter({f"{so} -> {sn}": n for (so, sn), n in trans.items() if so != sn}), out)

    # Contenu detecte des fichiers dont le statut change (comprendre pourquoi)
    chg = Counter()
    for k in communs:
        if cle_anc[k]["statut"] != cle_nou[k]["statut"]:
            chg[(cle_anc[k]["statut"], cle_nou[k]["statut"], modalites(cle_nou[k].get("contenu_detecte", "")))] += 1
    tableau("Changements de statut : ancien | nouveau | contenu detecte", chg, out)

    sa, sn = synthese(a.ancien), synthese(a.nouveau)
    out.append("")
    out.append("INDICATEURS COMPLETS (valeurs entieres, non tronquees)")
    for ind in sn:
        if ind.startswith(("Images par", "Qualit", "Images mises", "Nombre total", "Nombre de patients avec")):
            out.append(f"- {ind}")
            out.append(f"    ancien  : {sa.get(ind, '(absent)')}")
            out.append(f"    nouveau : {sn[ind]}")

    texte = "\n".join(out)
    print(texte)
    Path("comparaison_passages.txt").write_text(texte + "\n", encoding="utf-8")
    print("\nEcrit dans comparaison_passages.txt")


if __name__ == "__main__":
    main()
