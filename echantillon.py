# -*- coding: utf-8 -*-
"""
echantillon.py — Tire un échantillon de PATIENTS, par centre, qui couvre les types d'images
=====================================================================================

Pourquoi : traiter toutes les images de tous les centres prend des dizaines d'heures. Pour une
exécution de test, ou pour les mesures d'un article, on veut un sous-ensemble défini à l'avance,
REPRODUCTIBLE (graine notée) et qui représente la diversité réelle : les centres n'ont pas tous
les mêmes formats ni les mêmes modalités.

Le tirage part de l'inventaire exhaustif (01_inventaire/contenu_images.csv, produit par une
simulation ou un traitement) :

    python fredd.py echantillon --patients-par-centre 50 --min-par-type 10 --graine 2026

Règles :
  - l'unité est le PATIENT : toutes ses images suivent ;
  - une STRATE = (centre, catégorie de fichier, modalité) ; par centre, chaque strate présente
    reçoit au moins --min-par-type patients (ou tous ceux qui existent, s'il y en a moins) ;
    les strates les plus rares sont servies en premier ;
  - le reste est complété AU HASARD jusqu'à --patients-par-centre ;
  - les modalités que le pipeline ne traite pas (par défaut infrarouge et raw_data) sont ignorées :
    elles ne servent pas à la couverture (--exclure-modalites pour en changer) ;
  - si la couverture demande plus de patients que --patients-par-centre, tous les patients de
    couverture sont gardés (l'effectif réel est alors plus grand : voir echantillon_effectifs.csv).

Sorties (dossier echantillon/ à côté de 01_inventaire/) :
  - echantillon_patients.csv   : centre ; id_fredd ; raison (couverture / aleatoire) ; types
  - echantillon_effectifs.csv  : centre ; type ; patients disponibles ; retenus ; images retenues ;
                                 strate sous le minimum (oui = moins de patients disponibles que le minimum)
  - echantillon_parametres.json: graine, paramètres, date, empreinte de l'inventaire source

À savoir pour l'interprétation : l'échantillon de COUVERTURE sur-représente les types rares ; il
sert à montrer que le pipeline fonctionne sur toute la diversité, PAS à estimer des taux. Pour
estimer des taux (images « à vérifier », fuites…), il faut un tirage aléatoire sans sur-
représentation, ou pondérer par les effectifs de echantillon_effectifs.csv.

Pour traiter seulement cet échantillon : renseigner « echantillon = <chemin>\\echantillon_patients.csv »
dans la section [options] de config_fredd.ini (ou --echantillon avec pipeline_fredd.py).
"""

import argparse
import csv
import hashlib
import json
import random
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

FICHIER_CONTENU = Path("01_inventaire") / "contenu_images.csv"
DOSSIER_SORTIE = "echantillon"
ENTETE_PATIENTS = ["centre", "id_fredd", "raison", "types"]
ENTETE_EFFECTIFS = ["centre", "type", "patients_disponibles", "patients_retenus", "images_retenues",
                    "strate_sous_le_minimum"]
MODALITES_EXCLUES_PAR_DEFAUT = ("infrarouge", "raw_data")


def _lire(path):
    with open(path, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f, delimiter=";"))


def _ecrire(path, entete, lignes):
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(entete)
        for r in lignes:
            w.writerow([r.get(h, "") for h in entete])


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def _classe_orientation(orientation):
    o = (orientation or "").strip().lower()
    if o.startswith("volume"):
        return "pile"
    for mot, classe in (("horizontale", "horizontale"), ("verticale", "verticale"), ("radiale", "radiale"),
                        ("oblique", "oblique")):
        if mot in o:
            return classe
    return "orientation_inconnue"


def _type(r, detail=False):
    base = f"{(r.get('categorie') or '?').strip()}:{(r.get('modalite') or 'inconnue').strip()}"
    if not detail:
        return base
    # Strates détaillées : pour couvrir, dans l'échantillon de test, les OCT horizontaux / verticaux /
    # piles / radiales et les familles d'appareils (Heidelberg, Optos, Zeiss...). Le champ des AF (30° / 55°)
    # ne se lit que sur les pixels traités : il n'est pas stratifiable ici.
    if (r.get("modalite") or "").strip() == "oct_bscan":
        return base + ":" + _classe_orientation(r.get("orientation"))
    appareil = (r.get("appareil") or "").strip().split()
    return base + (":" + appareil[0].lower() if appareil else "")


def _retenue(r, modalites_exclues):
    pid = (r.get("id_fredd") or "").strip()
    if not pid or r.get("statut_tri") == "IGNOREE_DOUBLON" or (r.get("erreur") or "").strip():
        return False
    return (r.get("modalite") or "inconnue").strip() not in modalites_exclues


def types_par_patient(lignes, modalites_exclues=(), detail=False):
    """{centre: {id_fredd: {type, ...}}} d'après contenu_images.csv.
    Écartés : images sans patient FREDD, doublons binaires, fichiers illisibles, modalités exclues."""
    res = defaultdict(lambda: defaultdict(set))
    for r in lignes:
        if _retenue(r, modalites_exclues):
            res[(r.get("centre") or "").strip()][(r.get("id_fredd") or "").strip()].add(_type(r, detail))
    return res


def images_par_strate(lignes, modalites_exclues=(), detail=False):
    """{(centre, type): {id_fredd: nombre d'images}} : sert à compter les images de l'échantillon."""
    res = defaultdict(lambda: defaultdict(int))
    for r in lignes:
        if _retenue(r, modalites_exclues):
            res[((r.get("centre") or "").strip(), _type(r, detail))][(r.get("id_fredd") or "").strip()] += 1
    return res


def tirer(lignes, patients_par_centre, min_par_type, graine, modalites_exclues=(), detail=False):
    """Retourne (patients, effectifs) : listes de dicts pour les deux CSV.
    modalites_exclues : modalités ignorées (ni couverture, ni comptage) car le pipeline ne les traite pas."""
    if patients_par_centre < 1 or min_par_type < 0:
        raise ValueError("--patients-par-centre doit être ≥ 1 et --min-par-type ≥ 0.")
    patients, effectifs = [], []
    images = images_par_strate(lignes, modalites_exclues, detail)
    for centre, pats in sorted(types_par_patient(lignes, modalites_exclues, detail).items()):
        rng = random.Random(f"{graine}|{centre}")
        strates = defaultdict(list)
        for pid in sorted(pats):
            for t in pats[pid]:
                strates[t].append(pid)
        retenus = {}
        # 1. couverture : strates les plus rares d'abord (un patient peut en servir plusieurs)
        for t in sorted(strates, key=lambda x: (len(strates[x]), x)):
            visees = min(min_par_type, len(strates[t]))
            deja = sum(1 for p in strates[t] if p in retenus)
            if deja < visees:
                candidats = [p for p in strates[t] if p not in retenus]
                rng.shuffle(candidats)
                for p in candidats[:visees - deja]:
                    retenus[p] = "couverture"
        # 2. complément aléatoire
        manque = patients_par_centre - len(retenus)
        if manque > 0:
            reste = [p for p in sorted(pats) if p not in retenus]
            rng.shuffle(reste)
            for p in reste[:manque]:
                retenus[p] = "aleatoire"
        for p in sorted(retenus):
            patients.append({"centre": centre, "id_fredd": p, "raison": retenus[p],
                             "types": " | ".join(sorted(pats[p]))})
        for t in sorted(strates):
            n = sum(1 for p in strates[t] if p in retenus)
            nb_images = sum(v for p, v in images[(centre, t)].items() if p in retenus)
            effectifs.append({"centre": centre, "type": t, "patients_disponibles": len(strates[t]),
                              "patients_retenus": n, "images_retenues": nb_images,
                              "strate_sous_le_minimum": "oui" if len(strates[t]) < min_par_type else "non"})
    return patients, effectifs


def lire_echantillon(path):
    """Ensemble des id_fredd d'un echantillon_patients.csv (utilisé par pipeline_fredd --echantillon)."""
    lignes = _lire(path)
    if not lignes or "id_fredd" not in lignes[0]:
        raise ValueError(f"{path} : colonne 'id_fredd' absente ou fichier vide.")
    return {(r["id_fredd"] or "").strip() for r in lignes if (r.get("id_fredd") or "").strip()}


def echantillonner(sortie, patients_par_centre, min_par_type, graine, log=print,
                   modalites_exclues=MODALITES_EXCLUES_PAR_DEFAUT, detail=False):
    """sortie = dossier contenant 01_inventaire/contenu_images.csv. Retourne 0 ou 2."""
    src = Path(sortie) / FICHIER_CONTENU
    if not src.is_file():
        log(f"[ERREUR] {src} introuvable : lancer d'abord une simulation (menu 2) ou un traitement.")
        return 2
    lignes = _lire(src)
    try:
        patients, effectifs = tirer(lignes, patients_par_centre, min_par_type, graine, set(modalites_exclues), detail)
    except ValueError as e:
        log(f"[ERREUR] {e}")
        return 2
    if not patients:
        log("[ERREUR] Aucun patient FREDD avec des images lisibles dans l'inventaire.")
        return 2
    dest = Path(sortie) / DOSSIER_SORTIE
    dest.mkdir(parents=True, exist_ok=True)
    _ecrire(dest / "echantillon_patients.csv", ENTETE_PATIENTS, patients)
    _ecrire(dest / "echantillon_effectifs.csv", ENTETE_EFFECTIFS, effectifs)
    (dest / "echantillon_parametres.json").write_text(json.dumps({
        "graine": graine, "patients_par_centre": patients_par_centre, "min_par_type": min_par_type,
        "date": datetime.now().isoformat(timespec="seconds"), "inventaire_source": str(src),
        "inventaire_sha256": _sha256(src), "nb_patients": len(patients),
        "modalites_exclues": sorted(modalites_exclues), "strates_detaillees": bool(detail),
        "nb_images": sum(int(e["images_retenues"]) for e in effectifs),
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    par_centre = defaultdict(lambda: [0, 0])
    for p in patients:
        par_centre[p["centre"]][0] += 1
        par_centre[p["centre"]][1] += p["raison"] == "couverture"
    log(f"Échantillon tiré (graine {graine}) :")
    for c, (n, nc) in sorted(par_centre.items()):
        log(f"  {c or '(centre non renseigné)'} : {n} patient(s), dont {nc} retenu(s) pour la couverture")
    modalites = sorted({(r.get("modalite") or "inconnue").strip() for r in lignes})
    log(f"Modalités présentes dans l'inventaire : {', '.join(modalites)}")
    log(f"Modalités exclues de l'échantillon : {', '.join(sorted(modalites_exclues)) or '(aucune)'}")
    log(f"Images de l'échantillon : {sum(int(e['images_retenues']) for e in effectifs)}")
    rares = [e for e in effectifs if e["strate_sous_le_minimum"] == "oui"]
    if rares:
        log(f"[ATTENTION] {len(rares)} strate(s) avec moins de patients disponibles que le minimum "
            "(tous retenus) : voir echantillon_effectifs.csv")
    log(f"Fichiers : {dest}")
    log("Pour traiter seulement cet échantillon : « echantillon = "
        f"{dest / 'echantillon_patients.csv'} » dans config_fredd.ini, section [options].")
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(description="Échantillon de patients par centre, couvrant les types d'images.")
    p.add_argument("--sortie", required=True, help="Dossier contenant 01_inventaire/ (sortie ou sortie_simulation).")
    p.add_argument("--patients-par-centre", type=int, required=True, help="Effectif visé par centre.")
    p.add_argument("--min-par-type", type=int, required=True,
                   help="Minimum de patients par (centre, catégorie, modalité), à fixer avec les co-auteurs.")
    p.add_argument("--graine", type=int, required=True, help="Graine du tirage (notée dans les paramètres).")
    p.add_argument("--exclure-modalites", default=",".join(MODALITES_EXCLUES_PAR_DEFAUT),
                   help="Modalités ignorées par le tirage, séparées par des virgules (défaut : "
                        + ",".join(MODALITES_EXCLUES_PAR_DEFAUT) + "). Valeur vide = rien d'exclu.")
    p.add_argument("--strates-detaillees", action="store_true",
                   help="Ajoute l'orientation des OCT (horizontale, verticale, pile, radiale...) et la famille d'appareil "
                        "aux strates : pour un échantillon de TEST qui couvre ces cas.")
    a = p.parse_args(argv)
    exclues = tuple(m.strip() for m in a.exclure_modalites.split(",") if m.strip())
    return echantillonner(a.sortie, a.patients_par_centre, a.min_par_type, a.graine, modalites_exclues=exclues,
                          detail=a.strates_detaillees)


if __name__ == "__main__":
    sys.exit(main())
