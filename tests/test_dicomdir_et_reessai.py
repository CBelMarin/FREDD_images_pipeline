"""DICOMDIR = statut dédié ; réessai unitaire
aussi quand raretia rend le code 0 mais n'a produit aucune sortie pour un fichier."""
import io
import json

import pydicom

from conftest import FAKE_RARETIA, dicom_bytes, ecrire, lire_csv
import pipeline_fredd as pf

PID = "a1b2c3d4-0000-0000-0000-000000000001"


def dicomdir_bytes():
    ds = pydicom.dcmread(io.BytesIO(dicom_bytes(seed=5)))
    ds.file_meta.MediaStorageSOPClassUID = "1.2.840.10008.1.3.10"
    del ds.SOPClassUID
    buf = io.BytesIO()
    ds.save_as(buf, enforce_file_format=True)
    return buf.getvalue()


def _lancer(tmp_path, *extra, nopix=False):
    racine = tmp_path / "entree" / "CentreA"
    ecrire(racine / PID / f"{PID}_OD.dcm", dicom_bytes(seed=1))
    ecrire(racine / PID / "DICOMDIR", dicomdir_bytes())
    if nopix:
        ecrire(racine / PID / f"{PID}_OG.dcm", dicom_bytes(comments="NOPIX", seed=2))
    data = tmp_path / "d.csv"
    data.write_text(f"id;centre;img_retino\n{PID};CentreA;oui\n", encoding="utf-8")
    sortie = tmp_path / "sortie"
    assert pf.main([str(tmp_path / "entree"), "--data", str(data), "--sortie", str(sortie),
                    "--raretia-path", str(FAKE_RARETIA), "--cols-skezia", "img_retino:fundus",
                    "--col-centre", "centre", *extra]) == 0
    return sortie


def _par_nom(sortie):
    return {l["chemin_source"].replace("\\", "/").split("/")[-1].split(" :: ")[-1]: l
            for l in lire_csv(sortie / "rapports" / "journal_images.csv")}


def test_dicomdir_statut_dedie_non_envoye_et_pas_une_image(tmp_path):
    sortie = _lancer(tmp_path)
    j = _par_nom(sortie)
    assert j["DICOMDIR"]["statut"] == "NON_TRAITEE_DICOMDIR"
    assert j["DICOMDIR"]["destination"].replace("\\", "/").startswith("non_traitees_format/CentreA/_dicomdir/")
    assert not (sortie / "echecs_traitement").exists()
    conf = lire_csv(sortie / "rapports" / "confrontation_patients.csv")[0]
    assert conf["nb_echecs"] == "0"


def test_dicomdir_sans_analyse_contenu_requalifie(tmp_path):
    sortie = _lancer(tmp_path, "--pas-analyse-contenu")
    assert _par_nom(sortie)["DICOMDIR"]["statut"] == "NON_TRAITEE_DICOMDIR"


def test_reessai_unitaire_aussi_a_code_zero_mais_pas_pour_les_dicomdir(tmp_path):
    sortie = _lancer(tmp_path, nopix=True)
    paquets = lire_csv(sortie / "rapports" / "paquets_raretia.csv")
    assert paquets[0]["code_retour"] == "0"
    assert paquets[0]["reessai_unitaire"] == "1 image(s)"        # le NOPIX seulement, pas le DICOMDIR
    j = _par_nom(sortie)
    assert j[f"{PID}_OG.dcm"]["statut"] == "ECHEC_TRAITEMENT"      # le réessai confirme : échec déterministe
