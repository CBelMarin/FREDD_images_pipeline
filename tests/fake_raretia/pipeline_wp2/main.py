"""Faux `python -m pipeline_wp2.main <entrée> -o <sortie>` pour les tests.

Comportements pilotés par le tag ImageComments du DICOM source :
  CRASH  -> le processus entier plante (code retour 1, aucune sortie)
  NOPIX  -> image ignorée silencieusement (rejet d'entrée documenté)
SOP Raw Data Storage -> ignorée, comme dans le vrai module pipeline_wp2
  FLAG   -> image produite mais marquée « À VÉRIFIER »
  LEAK   -> image produite SANS vider PatientName (défaut de pseudonymisation)
NumberOfFrames = N -> N images produites. Un .e2e produit 3 B-scans OCT, plus une image par
série en face non IR (AF -> autofluorescence), comme le vrai code.
"""
import argparse
import csv
import sys
from pathlib import Path

import numpy as np
import pydicom
from pydicom.dataset import FileDataset, FileMetaDataset
from pydicom.uid import ExplicitVRLittleEndian, generate_uid

HEADERS = ["source_file", "image_index", "image_type", "original_shape", "processed_shape", "laterality",
           "angle_of_view", "optic_disc", "fovea", "quality_score", "quality_issues", "cropped",
           "accepted", "rejection_reason", "output_file"]


def ecrire_sc(path, patient_id, patient_name="", commentaire=""):
    meta = FileMetaDataset()
    meta.MediaStorageSOPClassUID = "1.2.840.10008.5.1.4.1.1.7"
    meta.MediaStorageSOPInstanceUID = generate_uid()
    meta.TransferSyntaxUID = ExplicitVRLittleEndian
    ds = FileDataset(str(path), {}, file_meta=meta, preamble=b"\0" * 128)
    ds.SOPClassUID = meta.MediaStorageSOPClassUID
    ds.SOPInstanceUID = meta.MediaStorageSOPInstanceUID
    ds.PatientName = patient_name
    ds.PatientID = patient_id
    ds.ImageType = ["DERIVED", "SECONDARY", "PREPROCESSED"]
    ds.Modality = "OT"
    if commentaire:
        ds.ImageComments = commentaire
    ds.Rows = ds.Columns = 8
    ds.SamplesPerPixel = 1
    ds.PhotometricInterpretation = "MONOCHROME2"
    ds.BitsAllocated = ds.BitsStored = 8
    ds.HighBit = 7
    ds.PixelRepresentation = 0
    ds.PixelData = np.zeros((8, 8), dtype=np.uint8).tobytes()
    ds.save_as(str(path), enforce_file_format=True)


def _modalites_e2e(f):
    """Modalités des séries de l'E2E, via l'analyse de contenu du pipeline
    (le faux raretia ne décode pas les E2E)."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
    try:
        import analyse_contenu as ac
        return [l["modalite"] for l in ac.analyser_e2e(f) if not l.get("erreur")]
    except Exception:  # noqa: BLE001
        return []


def main():
    p = argparse.ArgumentParser()
    p.add_argument("entree")
    p.add_argument("-o", "--output", required=True)
    p.add_argument("--target-size", default=None)
    a = p.parse_args()

    entree = Path(a.entree)
    fichiers = sorted(entree.rglob("*")) if entree.is_dir() else [entree]
    fichiers = [f for f in fichiers if f.is_file()]
    sortie = Path(a.output) / entree.name
    sortie.mkdir(parents=True, exist_ok=True)

    plan = []
    for k, f in enumerate(fichiers):
        print(k, "/", len(fichiers), " - ", f, flush=True)  # comme le vrai pipeline_wp2/pipeline.py
        if f.suffix.lower() == ".e2e":
            # Comme le vrai code : les B-scans OCT, plus une image par série en face
            # (AF étiquetée autofluorescence), IR exclues. 3 B-scans fixes pour l'OCT.
            mods = _modalites_e2e(f)
            if not mods or "oct_bscan" in mods:
                plan.append((f, "", 3, "oct_bscan", "", "R"))
            for m in mods:
                if m == "autofluorescence":
                    plan.append((f, "", 1, "autofluorescence", "", "R"))
                elif m not in ("oct_bscan", "infrarouge", "inconnu"):
                    plan.append((f, "", 1, "fundus_macular", "", "R"))
            continue
        if f.suffix.lower() != ".dcm":
            print(f"[WARN] format ignoré : {f.name}")
            continue
        ds = pydicom.dcmread(str(f), force=True)
        com = str(getattr(ds, "ImageComments", ""))
        if "CRASH" in com:
            print(f"Traceback (most recent call last):\nRuntimeError: decodage impossible {f.name}", file=sys.stderr)
            sys.exit(1)
        if "NOPIX" in com:
            continue
        if str(getattr(ds, "SOPClassUID", "")) == "1.2.840.10008.5.1.4.1.1.66":
            continue  # comme le vrai pipeline_wp2/reader.py : « Fichier Raw Data ignoré »
        if "1.2.840.10008.1.3.10" in (str(getattr(ds, "SOPClassUID", "")),
                                      str(getattr(getattr(ds, "file_meta", None), "MediaStorageSOPClassUID", ""))):
            continue  # un DICOMDIR n'a pas de pixels : le vrai code ne produit aucune sortie (code 0)
        n = int(getattr(ds, "NumberOfFrames", 1) or 1)
        it = "/".join(getattr(ds, "ImageType", []) or [])
        typ = "autofluorescence" if any(k in it.upper() for k in ("AF", "FAF", "AUTOFLUOR")) else \
            "oct_bscan" if ds.Modality in ("OPT", "OCT") else (
            "fundus_color" if int(getattr(ds, "SamplesPerPixel", 1)) == 3 else "fundus_macular")
        lat = str(getattr(ds, "ImageLaterality", "") or "R")
        plan.append((f, com, n, typ, str(ds.PatientName) if "LEAK" in com else "", lat))

    lignes = []
    for item in plan:
        f, com, n, typ, nom_fuite = item[:5]
        lat = item[5] if len(item) > 5 else "R"
        for i in range(n):
            out = sortie / f"{f.stem}_{i}_{typ}.dcm"
            # Comme le vrai code (v1.12.0) : l'AF porte son champ dans ImageComments. Ici : maculaire,
            # sauf si l'image source porte « CHAMP_GC » (grand champ).
            champ = ("champ : " + ("grand_champ" if "CHAMP_GC" in com else "maculaire") + " (méthode : coins)"
                     if typ == "autofluorescence" else "")
            ecrire_sc(out, f.stem, nom_fuite, champ)
            flag = "FLAG" in com
            lignes.append([f.name, i, typ, "(8, 8)", "(8, 8)", lat, 45, "", "", "0.210" if flag else "0.812",
                           "Image trop sombre" if flag else "", "OUI", "À VÉRIFIER" if flag else "OUI",
                           "Image trop sombre (médiane 40)" if flag else "", out.name])
    with open(sortie / "rapport_pretraitement.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh, delimiter=";")
        w.writerow(HEADERS)
        w.writerows(lignes)
    print(f"{len(lignes)} image(s) traitée(s)")


if __name__ == "__main__":
    main()
