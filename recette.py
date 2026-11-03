#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
recette.py — validation du pipeline sur un ÉCHANTILLON RÉEL de fichiers FREDD
==============================================================================

Les tests automatiques (dossier tests/) prouvent la logique sur des fichiers
synthétiques. Seule une recette sur de vrais fichiers prouve que les vrais
E2E/DICOM sont bien lus. Principe : vous décrivez ce que CONTIENT chaque
fichier d'un échantillon (vérité terrain), le pipeline est lancé en
simulation, et chaque fichier est comparé critère par critère.

ÉTAPE 1 — générer le modèle à remplir
    python recette.py modele "D:\\echantillon" --sortie verite_terrain.csv

  Le modèle liste les fichiers images, avec des colonnes ATTENDUES VIDES.
  Elles sont volontairement vides : un modèle pré-rempli par le pipeline
  pousserait à valider ce qu'il affiche au lieu de vérifier le fichier
  (biais de confirmation). Remplissez en ouvrant chaque fichier dans
  HEYEX / la visionneuse DICOM :

    id_attendu       ID SKEZIA complet ou 8 premiers caractères ;
                     « - » si l'image doit être MISE DE CÔTÉ (pas de patient).
    contenu_attendu  séries séparées par « ; », chacune « modalité [œil] [orientation] »
                     modalités : oct_bscan, autofluorescence, fundus, infrarouge,
                                 angiographie_fluo, icga, fundus_multicolor, image_standard
                     œil : OD / OG ; orientation (OCT) : horizontale, verticale,
                     oblique, volume (lignes horizontales/verticales), radiale
                     ex. « oct_bscan OD horizontale; autofluorescence OD »
    Une cellule laissée vide = critère non vérifié pour ce fichier.

ÉTAPE 2 — vérifier
    python recette.py verifier "D:\\echantillon" --data export.csv --verite verite_terrain.csv ^
           --col-id id --sortie D:\\recette_2026-10-15

  Produit recette_resultats.csv (une ligne par fichier et par critère :
  attendu / obtenu / OK / ÉCART) et un résumé. Code de sortie 0 si tout est
  conforme, 1 sinon. Les séries détectées EN TROP sont aussi des écarts.
"""

import argparse
import csv
import os
import sys
import tempfile
from collections import Counter
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ICI = Path(__file__).resolve().parent
sys.path.insert(0, str(ICI))

import inventaire_images as inv  # noqa: E402
import pipeline_fredd as pf  # noqa: E402

COLONNES_MODELE = ["chemin_relatif", "id_attendu", "contenu_attendu", "commentaire"]
OEILS = {"OD", "OG"}
ORIENTATIONS = {"horizontale", "verticale", "oblique", "radiale", "radiale/mixte",
                "volume", "volume, lignes horizontales", "volume, lignes verticales"}


def _relatif(chemin, racines):
    for r in racines:
        r = str(Path(r).resolve())
        if chemin.startswith(r):
            return Path(r).name + chemin[len(r):].replace("\\", "/")
    return chemin.replace("\\", "/")


def lister_images(dossiers):
    """Inventaire seul (sans copie, sans hash) -> chemins relatifs des images."""
    with tempfile.TemporaryDirectory() as tmp:
        args = argparse.Namespace(extensions_image_suppl="", profondeur_zip_max=10, taille_max_zip_memoire_mo=200,
                                  pas_de_zip=False, pas_de_hash=True)
        with open(Path(tmp) / "i.csv", "w", newline="", encoding="utf-8") as fi, \
             open(Path(tmp) / "e.csv", "w", newline="", encoding="utf-8") as fe:
            wi, we = csv.writer(fi, delimiter=";"), csv.writer(fe, delimiter=";")
            wi.writerow(inv.INVENTORY_HEADERS)
            ctx = inv.Contexte(args, wi, we, set())
            for d in dossiers:
                inv.scan_root(Path(d), ctx)
        with open(Path(tmp) / "i.csv", encoding="utf-8") as f:
            return sorted(_relatif(r["chemin"], dossiers) for r in csv.DictReader(f, delimiter=";"))


def cmd_modele(a):
    chemins = lister_images(a.dossiers)
    with open(a.sortie, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(COLONNES_MODELE)
        for c in chemins:
            w.writerow([c, "", "", ""])
    print(f"{len(chemins)} fichier(s) listé(s) dans {a.sortie}. Remplir id_attendu et contenu_attendu "
          f"en ouvrant chaque fichier, puis lancer « recette.py verifier ».")
    return 0


def parse_serie(txt):
    """'oct_bscan OD volume, lignes horizontales' -> (modalite, oeil|None, orientation|None)."""
    mots = txt.strip().split()
    if not mots:
        return None
    mod, oeil, reste = mots[0].lower(), None, mots[1:]
    if reste and reste[0].upper() in OEILS:
        oeil, reste = reste[0].upper(), reste[1:]
    orient = " ".join(reste).lower() or None
    if orient and orient not in ORIENTATIONS:
        raise ValueError(f"orientation inconnue « {orient} » (attendu : {', '.join(sorted(ORIENTATIONS))})")
    return mod, oeil, orient


def _correspond(att, obt):
    mod, oeil, orient = att
    m_ok = obt["modalite"] == mod or obt["modalite"].startswith(mod + "_") or (
        mod == "fundus" and obt["modalite"].startswith("fundus"))
    o_ok = oeil is None or obt["lateralite"] == oeil
    if orient is None:
        r_ok = True
    elif orient == "volume":
        r_ok = obt["orientation"].startswith("volume")
    elif orient == "radiale":
        r_ok = obt["orientation"].startswith("radiale")
    else:
        r_ok = obt["orientation"] == orient
    return m_ok and o_ok and r_ok


def apparier(attendues, obtenues):
    """Appariement glouton 1-pour-1 (les attendues les plus précises d'abord).
    Retourne (non_trouvees, en_trop)."""
    restantes = list(obtenues)
    non_trouvees = []
    for att in sorted(attendues, key=lambda x: -sum(v is not None for v in x)):
        i = next((k for k, o in enumerate(restantes) if _correspond(att, o)), None)
        if i is None:
            non_trouvees.append(att)
        else:
            restantes.pop(i)
    return non_trouvees, restantes


def _fmt(c):
    return " ".join(x for x in (c["modalite"], c["lateralite"], c["orientation"]) if x) + \
        (f" [⚠ {c['alerte']}]" if c.get("alerte") else "")


def cmd_verifier(a):
    with open(a.verite, encoding="utf-8-sig", newline="") as f:
        echant = sep = f.read(4096)
        f.seek(0)
        verite = list(csv.DictReader(f, delimiter=";" if sep.count(";") >= sep.count(",") else ","))
    sortie = Path(a.sortie)
    simu = sortie / "simulation"
    argv = [*a.dossiers, "--data", str(a.data), "--sortie", str(simu), "--simulation"]
    for opt in ("col_id", "col_centre", "cols_skezia"):
        if getattr(a, opt):
            argv += [f"--{opt.replace('_', '-')}", getattr(a, opt)]
    if pf.main(argv) != 0:
        print("[ERREUR] Le pipeline n'a pas pu s'exécuter (voir messages ci-dessus).")
        return 2

    journal = {_relatif(r["chemin_source"], a.dossiers): r
               for r in csv.DictReader(open(simu / "rapports" / "journal_images.csv", encoding="utf-8-sig"),
                                       delimiter=";")}
    contenu = {}
    for c in csv.DictReader(open(simu / "01_inventaire" / "contenu_images.csv", encoding="utf-8-sig"), delimiter=";"):
        contenu.setdefault(_relatif(c["chemin_source"], a.dossiers), []).append(c)

    lignes, bilan = [], Counter()

    def noter(chemin, critere, attendu, obtenu, ok, detail=""):
        lignes.append(dict(chemin_relatif=chemin, critere=critere, attendu=attendu, obtenu=obtenu,
                           resultat="OK" if ok else "ÉCART", detail=detail))
        bilan[(critere, ok)] += 1

    for v in verite:
        chemin = v["chemin_relatif"].strip().replace("\\", "/")
        if not chemin:
            continue
        j = journal.get(chemin)
        if j is None:
            noter(chemin, "inventaire", "fichier inventorié", "ABSENT de l'inventaire", False)
            continue
        noter(chemin, "inventaire", "fichier inventorié", "inventorié", True)

        id_att = (v.get("id_attendu") or "").strip().lower()
        if id_att:
            if id_att == "-":
                ok = not j["id_fredd"]
                noter(chemin, "identifiant", "mis de côté (pas de patient)",
                      j["id_fredd"] or j["statut"], ok, j["observation"])
            else:
                obt = j["id_fredd"].lower()
                ok = bool(obt) and (obt == id_att or (len(id_att) == 8 and obt.startswith(id_att)))
                noter(chemin, "identifiant", id_att, obt or j["statut"], ok, j["observation"])

        txt = (v.get("contenu_attendu") or "").strip()
        if txt:
            if j["statut"] == "IGNOREE_DOUBLON":
                noter(chemin, "contenu", txt, "(copie binaire d'un autre fichier : contenu non relu)", True,
                      j.get("motif", ""))
                continue
            try:
                attendues = [s for s in (parse_serie(x) for x in txt.split(";")) if s]
            except ValueError as e:
                noter(chemin, "contenu", txt, f"vérité terrain mal saisie : {e}", False)
                continue
            obtenues = [c for c in contenu.get(chemin, []) if not c.get("erreur")]
            erreurs = [c["erreur"] for c in contenu.get(chemin, []) if c.get("erreur")]
            manquantes, en_trop = apparier(attendues, obtenues)
            ok = not manquantes and not en_trop
            detail = "; ".join(filter(None, [
                ("non retrouvé : " + ", ".join(" ".join(x for x in s if x) for s in manquantes)) if manquantes else "",
                ("en trop : " + ", ".join(_fmt(c) for c in en_trop)) if en_trop else "",
                ("illisible : " + " | ".join(erreurs)) if erreurs else ""]))
            noter(chemin, "contenu", txt, " ; ".join(_fmt(c) for c in obtenues) or "(rien)", ok, detail)

    sortie.mkdir(parents=True, exist_ok=True)
    res = sortie / "recette_resultats.csv"
    with open(res, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=["chemin_relatif", "critere", "attendu", "obtenu", "resultat", "detail"],
                           delimiter=";")
        w.writeheader()
        w.writerows(lignes)

    print("\n" + "=" * 70 + "\n  RECETTE\n" + "=" * 70)
    ecarts = 0
    for critere in ("inventaire", "identifiant", "contenu"):
        ok, ko = bilan[(critere, True)], bilan[(critere, False)]
        if ok + ko:
            ecarts += ko
            print(f"  {critere:<12} {ok}/{ok + ko} conformes" + (f"  — {ko} ÉCART(S)" if ko else ""))
    for l in lignes:
        if l["resultat"] != "OK":
            print(f"  ✗ {l['chemin_relatif']} [{l['critere']}] attendu « {l['attendu']} », obtenu « {l['obtenu']} »"
                  + (f" — {l['detail']}" if l["detail"] else ""))
    print(f"\n  Détail : {res}")
    verifies = sum(n for (critere, _ok), n in bilan.items() if critere != "inventaire")
    if not verifies and not ecarts:
        # Une vérité terrain vide ou mal remplie ne doit JAMAIS conclure « conforme ».
        print("  RÉSULTAT : AUCUN CRITÈRE VÉRIFIÉ — colonnes id_attendu / contenu_attendu vides "
              "ou chemins non reconnus. Recette NON CONCLUANTE.")
        return 2
    print("  RÉSULTAT : " + ("CONFORME" if not ecarts else f"{ecarts} ÉCART(S) — ne pas lancer le volume complet")
          + f" ({verifies} critère(s) vérifié(s))")
    return 0 if not ecarts else 1


def main(argv=None):
    p = argparse.ArgumentParser(description="Recette du pipeline FREDD sur un échantillon réel (voir l'aide du module).")
    sp = p.add_subparsers(dest="cmd", required=True)
    m = sp.add_parser("modele", help="Générer le modèle de vérité terrain à remplir.")
    m.add_argument("dossiers", nargs="+")
    m.add_argument("--sortie", default="verite_terrain.csv")
    v = sp.add_parser("verifier", help="Comparer le pipeline à la vérité terrain.")
    v.add_argument("dossiers", nargs="+")
    v.add_argument("--data", required=True, type=Path)
    v.add_argument("--verite", required=True, type=Path)
    v.add_argument("--sortie", required=True)
    v.add_argument("--col-id")
    v.add_argument("--col-centre")
    v.add_argument("--cols-skezia")
    a = p.parse_args(argv)
    a.dossiers = [str(Path(d).resolve()) for d in a.dossiers]
    return cmd_modele(a) if a.cmd == "modele" else cmd_verifier(a)


if __name__ == "__main__":
    sys.exit(main())
