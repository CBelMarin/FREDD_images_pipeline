# -*- coding: utf-8 -*-
"""
audit_sorties_dicom.py : controle independant des DICOM livres (images_traitees\\), EN-TETES SEULEMENT.

Aucun chemin, aucun nom, aucun identifiant n'est affiche : seulement des effectifs et des noms de balises.
Pour chaque fichier : quelles balises sont presentes, lesquelles de la liste sensible sont non vides,
presence de balises privees, BurnedInAnnotation, coherence PatientID / dossier, syntaxe de transfert.

Usage :  python audit_sorties_dicom.py <dossier de sortie> [--pas 1]
(--pas 10 : un fichier sur dix, pour un essai rapide)
"""
import argparse
import sys
import warnings
from collections import Counter
from pathlib import Path

import pydicom

try:
    sys.stdout.reconfigure(errors="replace")
except Exception:
    pass

SENSIBLES = ["PatientName", "PatientBirthDate", "PatientAddress", "PatientTelephoneNumbers", "OtherPatientIDs",
             "OtherPatientNames", "PatientMotherBirthName", "MedicalRecordLocator", "AdditionalPatientHistory",
             "PatientComments", "InstitutionName", "InstitutionAddress", "InstitutionalDepartmentName",
             "ReferringPhysicianName", "PerformingPhysicianName", "OperatorsName", "PhysiciansOfRecord",
             "RequestingPhysician", "AccessionNumber", "StationName", "DeviceSerialNumber", "StudyID",
             "IssuerOfPatientID", "PatientAge", "PatientSize", "PatientWeight", "EthnicGroup", "Occupation"]


LIMITES = {"LO": 64, "SH": 16, "CS": 16, "PN": 64, "ST": 1024, "UI": 64, "DS": 16, "IS": 12, "LT": 10240}
APPAREIL = ["Manufacturer", "ManufacturerModelName", "SoftwareVersions"]


def vide(v):
    return v is None or str(v).strip() in ("", "None")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("sortie")
    ap.add_argument("--pas", type=int, default=1)
    a = ap.parse_args()
    racine = Path(a.sortie) / "images_traitees"
    fichiers = sorted(racine.rglob("*.dcm"))[::a.pas]
    n = len(fichiers)
    presentes, non_vides = Counter(), Counter()
    prive = burned = pid_diff = illisibles = 0
    ts, modal, dims, cles_comm, burned_val = Counter(), Counter(), Counter(), Counter(), Counter()
    trop_longs, appareils, annees, ps_par_modal, date_vide = Counter(), Counter(), Counter(), Counter(), 0
    for f in fichiers:
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                ds = pydicom.dcmread(str(f), stop_before_pixels=True, force=True)
                for el in ds:
                    if el.keyword in APPAREIL and not vide(el.value):
                        vs = el.value
                        multi = not isinstance(vs, (str, bytes)) and hasattr(vs, "__iter__")
                        liste = [str(x) for x in vs] if multi else [str(vs)]
                        etiquette = " \\ ".join(x if len(x) <= 64 else f"[{len(x)} car.]" for x in liste)
                        if len(liste) > 1:
                            etiquette = f"{len(liste)} valeurs, longueur max {max(len(x) for x in liste)}"
                        appareils[(el.keyword, etiquette)] += 1
                    lim = LIMITES.get(el.VR)
                    if lim and el.keyword and el.keyword != "ImageComments":
                        vals = el.value if isinstance(el.value, (list, tuple)) or hasattr(el.value, "__iter__") and not isinstance(el.value, (str, bytes)) else [el.value]
                        if any(len(str(x)) > lim for x in vals):
                            trop_longs[(el.keyword, el.VR)] += 1
        except Exception:  # noqa: BLE001
            illisibles += 1
            continue
        sd = str(ds.get("StudyDate", "")).strip()
        if not sd:
            date_vide += 1
        else:
            annees[sd[:4]] += 1
        ps_par_modal[(str(ds.get("Modality", "?")), "PixelSpacing" in ds)] += 1
        for el in ds:
            if el.keyword:
                presentes[el.keyword] += 1
        if any(el.tag.is_private for el in ds):
            prive += 1
        for k in SENSIBLES:
            if k in ds and not vide(ds.get(k)):
                non_vides[k] += 1
        bi = str(ds.get("BurnedInAnnotation", "absent"))
        burned_val[bi] += 1
        if str(ds.get("PatientID", "")) != f.parent.name:
            pid_diff += 1
        ts[str(getattr(ds.file_meta, "TransferSyntaxUID", "?"))] += 1
        modal[str(ds.get("Modality", "?"))] += 1
        dims[f"{ds.get('Rows', '?')}x{ds.get('Columns', '?')}"] += 1
        for morceau in str(ds.get("ImageComments", "")).split(";"):
            if "=" in morceau:
                cles_comm[morceau.split("=")[0].strip()] += 1
    print(f"fichiers audites : {n} (illisibles : {illisibles})")
    print(f"PatientID different du dossier : {pid_diff}")
    print(f"fichiers avec balises privees : {prive}")
    print("BurnedInAnnotation :", dict(burned_val))
    print("balises sensibles NON VIDES (attendu : aucune) :", dict(non_vides) or "aucune")
    print("syntaxes de transfert :", dict(ts))
    print("modalites :", dict(modal))
    print("dimensions :", dict(dims))
    print("cles de ImageComments :", dict(cles_comm))
    print("elements depassant la longueur maximale de leur VR (balise, VR) :", dict(trop_longs) or "aucun")
    print("StudyDate vide :", date_vide, "| annees des StudyDate :", dict(sorted(annees.items())))
    print("PixelSpacing par (modalite, present) :", dict(ps_par_modal))
    print("appareils (balise, valeur) : nombre de fichiers")
    for k, v in sorted(appareils.items()):
        print(f"  {k}: {v}")
    print("balises presentes (nombre de fichiers) :")
    for k, v in sorted(presentes.items()):
        print(f"  {k}: {v}")


if __name__ == "__main__":
    main()
