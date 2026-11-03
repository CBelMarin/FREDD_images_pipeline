#!/usr/bin/env python3
"""Audit de nommage FREDD — liste tous les fichiers pour lesquels AUCUN
pseudonyme (ID SKEZIA, complet ou tronqué à 8 caractères) n'est détectable
dans le chemin, selon la même règle que l'étape 2 du pipeline.

Ne nécessite PAS le CSV des données FREDD : c'est un contrôle de nommage
pur, utile pour repérer en amont des fichiers reçus sans pseudonymisation
(ex. nommés par patronyme), avant même de savoir si l'ID appartient à un
patient connu. Un fichier peut aussi être « suspect » parce que l'ID du
dossier et celui du fichier se contredisent (ID_AMBIGU) : mêmes risques de
fond, signalés séparément dans la colonne `observation`.

Usage :
    python audit_noms_pseudonymes.py <dossier1> [<dossier2> ...] --sortie audit_noms.csv

Les dossiers peuvent être les mêmes que ceux passés à pipeline_fredd.py.
⚠ Un dossier bien nommé (ex. `9aa1f672/`) masque, pour le pipeline, un fichier
mal nommé à l'intérieur (`9aa1f672/<nom>.e2e` est rattaché via le dossier,
donc PAS `suspect`) -- mais le nom du FICHIER expose quand même un patronyme
réel. C'est pourquoi `expose_identite` est calculé sur le nom du fichier
indépendamment de la résolution du pseudonyme, et ne doit jamais être lu
comme un sous-ensemble de `suspect`.
"""
import argparse
import csv
import re
from pathlib import Path

import pipeline_fredd as pf

HEADERS = ["centre", "chemin_source", "pseudonyme_detecte", "observation", "suspect",
           "type_nom_suspect", "expose_identite"]

# UID DICOM natif (ex. "1.2.276.0.75.2.2.70.0.3.250924140742902.30915005184.242902_1") :
# pas de convention FREDD respectée, mais AUCUNE identité patient dans le nom -- à distinguer
# d'un nom de famille réel, qui est un problème de gouvernance des données bien plus grave.
_UID_DICOM = re.compile(r"^\d+(\.\d+){4,}(_\d+)?$")

# Hexadécimal ou UUID "pur" : uniquement des chiffres, des lettres a-f et des
# tirets/underscores de séparation. Un ID FREDD légitime (8 hexa ou UUID
# complet) peut contenir par hasard 3 lettres a-f consécutives ("aac" dans
# "e4aac04b") -- il ne faut PAS que ça déclenche un faux positif "patronyme".
_HEX_OU_UUID_PUR = re.compile(r"^[0-9a-f]{6,}([_-][0-9a-f]{2,}){0,4}$", re.IGNORECASE)


def ressemble_a_un_patronyme(nom_fichier):
    """Vrai si le nom du fichier (hors extension) ressemble à un nom de
    famille plutôt qu'à un identifiant FREDD, un UID DICOM ou un nom neutre
    d'appareil. Calculé sur le nom du FICHIER seul, jamais sur le dossier :
    un fichier peut être correctement rattaché via son dossier tout en
    exposant un patronyme dans son propre nom."""
    stem = Path(nom_fichier or "").stem
    if not stem or _UID_DICOM.match(stem) or _HEX_OU_UUID_PUR.match(stem):
        return False
    return bool(re.search(r"[A-Za-zÀ-ÖØ-öø-ÿ]{3,}", stem))


def type_nom_suspect(nom_fichier):
    stem = Path(nom_fichier or "").stem
    if not stem:
        return "autre"
    if _UID_DICOM.match(stem):
        return "uid_dicom_non_renomme"
    if ressemble_a_un_patronyme(nom_fichier):
        return "nom_probable_patient"
    return "autre"


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("dossiers", nargs="+", help="Dossier(s) à auditer (comme pipeline_fredd.py)")
    p.add_argument("--sortie", default="audit_noms_pseudonymes.csv")
    p.add_argument("--travail", default="_audit_noms_tmp",
                    help="Dossier temporaire pour les CSV d'inventaire intermédiaires (créé si absent)")
    opts_cli = p.parse_args(argv)

    opts = argparse.Namespace(extensions_image_suppl="", profondeur_zip_max=10,
                               taille_max_zip_memoire_mo=200)
    dossier_travail = Path(opts_cli.travail)
    lignes_inv, erreurs, inv_path = pf.etape1_inventaire(opts_cli.dossiers, dossier_travail, opts, set())

    lignes = []
    for ligne in lignes_inv:
        pid, obs, discordant = pf.pseudo_sous_centre(ligne["chemin"], ligne["racine_scan"])
        suspect = (not pid) or discordant
        nom_fichier = ligne.get("nom_fichier", "")
        lignes.append({
            "centre": pf.centre_depuis_lot(ligne["lot"]),
            "chemin_source": ligne["chemin"],
            "pseudonyme_detecte": pid,
            "observation": obs,
            "suspect": "OUI" if suspect else "",
            "type_nom_suspect": type_nom_suspect(nom_fichier) if suspect else "",
            # Indépendant de `suspect` : calculé même quand le dossier fournit un
            # pseudonyme valide, car le FICHIER peut quand même exposer un patronyme.
            "expose_identite": "OUI" if ressemble_a_un_patronyme(nom_fichier) else "",
        })

    with open(opts_cli.sortie, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=HEADERS, delimiter=";")
        w.writeheader()
        w.writerows(lignes)

    n_suspect = sum(1 for l in lignes if l["suspect"])
    n_uid = sum(1 for l in lignes if l["type_nom_suspect"] == "uid_dicom_non_renomme")
    n_nom_suspect = sum(1 for l in lignes if l["type_nom_suspect"] == "nom_probable_patient")
    n_autre = sum(1 for l in lignes if l["suspect"] and l["type_nom_suspect"] == "autre")
    n_expose = sum(1 for l in lignes if l["expose_identite"])
    n_expose_rattaches = sum(1 for l in lignes if l["expose_identite"] and not l["suspect"])
    print(f"{len(lignes)} fichier(s) inventorié(s) ({dossier_travail}/{inv_path.name} pour le détail brut).")
    print(f"{n_suspect} fichier(s) SUSPECT(S) (aucun pseudonyme détecté, ou ID discordant dossier/fichier), dont :")
    print(f"  - {n_nom_suspect} nom(s) probable(s) de patient (patronyme), non rattachés")
    print(f"  - {n_uid} export(s) DICOM natif non renommé (UID appareil) -> pas d'identité exposée, juste "
          f"non conforme à la convention")
    print(f"  - {n_autre} autre(s) (nom neutre/aléatoire, sans ID reconnaissable)")
    print(f"\n{n_expose} fichier(s) EXPOSENT UN PATRONYME DANS LEUR NOM (colonne 'expose_identite'), "
          f"que le dossier fournisse ou non un pseudonyme valide, dont {n_expose_rattaches} déjà rattaché(s) "
          f"via un dossier correctement nommé (donc invisibles dans le décompte 'suspect' ci-dessus) -> "
          f"à remonter en priorité, c'est le vrai chiffre d'exposition d'identité.")
    if erreurs:
        print(f"{len(erreurs)} erreur(s) de lecture rencontrée(s) pendant l'inventaire (voir {dossier_travail}).")
    print(f"Rapport : {Path(opts_cli.sortie).resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
