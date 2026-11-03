"""Intégration avec le VRAI module pipeline_wp2 (pipeline_wp2/ livré à côté de
pipeline_fredd.py), sans le faux raretia. Plus lent (traitement d'image réel)."""
import pydicom
import pytest

import pipeline_fredd as pf
from conftest import VRAI_IPP, VRAI_NOM, dicom_bytes, dossier_patient, ecrire, lire_csv

pytest.importorskip("cv2")

PID = "a1b2c3d4-0000-0000-0000-000000000001"


@pytest.fixture(scope="module")
def execution(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("wp2")
    racine = tmp / "entree"
    ecrire(racine / "CentreA" / PID / f"{PID}_OD.dcm", dicom_bytes(rgb=True, seed=3))
    data = tmp / "data.csv"
    data.write_text(f"id;centre;img_retino\n{PID};CentreA;oui\n", encoding="utf-8")
    sortie = tmp / "sortie"
    # Pas de --raretia-path : le défaut doit pointer sur le pipeline_wp2/ livré.
    rc = pf.main([str(racine), "--data", str(data), "--sortie", str(sortie),
                  "--cols-skezia", "img_retino:fundus", "--col-centre", "centre"])
    return rc, sortie


def test_dicom_traite_et_range(execution):
    rc, sortie = execution
    assert rc == 0
    journal = lire_csv(sortie / "rapports" / "journal_images.csv")
    assert [l["statut"] for l in journal] == ["TRAITEE_OK"], journal
    assert not (sortie / "quarantaine_pseudonymisation").exists()


def test_patientid_harmonise_et_aucune_fuite(execution):
    _, sortie = execution
    sorties = list(dossier_patient(sortie, "CentreA", PID).glob("*.dcm"))
    assert len(sorties) == 1
    ds = pydicom.dcmread(str(sorties[0]))
    assert ds.PatientID == PID          # et non "UNKNOWN" (défaut corrigé dans reader.py)
    assert str(ds.PatientName) == ""
    brut = sorties[0].read_bytes()
    for ident in (VRAI_IPP, VRAI_NOM, VRAI_NOM.split("^")[0]):
        assert ident.encode() not in brut


def test_version_tracee(execution):
    import json
    _, sortie = execution
    meta = json.loads((sortie / "rapports" / "execution.json").read_text(encoding="utf-8"))
    assert "fredd" in json.dumps(meta, ensure_ascii=False)


# ───────────── Exclusion des IR (décision FREDD) ─────────────

PID_E2E = "e5f6a7b8-0000-0000-0000-000000000005"
SERIES_MIXTES = [
    dict(serie=1, motif="Line", lat="R", bscans=[(0, 0, 10, 0)], n_oct=1, n_fond=1),  # OCT + IR de repérage
    dict(serie=2, en_face="IR", lat="R", n_fond=1),                                    # IR seule
    dict(serie=3, en_face="Autofluorescence", lat="R", n_fond=1),                      # AF : à garder
]


@pytest.fixture(scope="module")
def execution_ir(tmp_path_factory):
    from conftest import e2e_bytes
    tmp = tmp_path_factory.mktemp("wp2_ir")
    racine = tmp / "entree"
    ecrire(racine / "CentreA" / PID_E2E / f"{PID_E2E}_OD.e2e", e2e_bytes(SERIES_MIXTES, pixels=True))
    ecrire(racine / "CentreA" / PID_E2E / f"{PID_E2E}_IR.e2e",
           e2e_bytes([dict(serie=1, en_face="IR", lat="L", n_fond=1)], pixels=True))
    data = tmp / "data.csv"
    data.write_text(f"id;centre;img_oct\n{PID_E2E};CentreA;oui\n", encoding="utf-8")
    sortie = tmp / "sortie"
    rc = pf.main([str(racine), "--data", str(data), "--sortie", str(sortie),
                  "--cols-skezia", "img_oct:oct_bscan", "--col-centre", "centre"])
    assert rc == 0
    return sortie


def test_ir_exclues_af_et_oct_gardes(execution_ir):
    sorties = lire_csv(execution_ir / "rapports" / "sorties_traitees.csv")
    types = sorted(s["image_type"] for s in sorties if s["chemin_source"].endswith("_OD.e2e"))
    # 1 B-scan OCT + l'AF, étiquetée comme telle ; ni l'IR de repérage ni l'IR seule
    assert types == ["autofluorescence", "oct_bscan"], types


def test_af_e2e_porte_un_champ_et_l_oct_n_en_a_pas(execution_ir):
    """l'AF d'un E2E sort avec un champ (maculaire / grand_champ), lu dans ses pixels ; pas l'OCT."""
    sorties = [s for s in lire_csv(execution_ir / "rapports" / "sorties_traitees.csv")
               if s["chemin_source"].endswith("_OD.e2e")]
    af = [s for s in sorties if s["image_type"] == "autofluorescence"]
    assert len(af) == 1 and af[0]["champ"] in ("maculaire", "grand_champ")
    assert all(s["champ"] == "" for s in sorties if s["image_type"] == "oct_bscan")


def test_e2e_uniquement_ir_statut_dedie(execution_ir):
    j = {l["chemin_source"][-7:]: l for l in lire_csv(execution_ir / "rapports" / "journal_images.csv")}
    assert j["_IR.e2e"]["statut"] == "NON_TRAITEE_IR"
    assert "_infrarouge" in j["_IR.e2e"]["destination"]
    assert j["_OD.e2e"]["statut"] in ("TRAITEE_OK", "TRAITEE_QUALITE_LIMITEE")


def test_oct_non_extrait_signale(tmp_path):
    """OCT présent à l'inventaire mais non décodé par le module pipeline_wp2 (ici
    B-scan tronqué ; sous Windows, une date d'acquisition nulle a le même
    effet) : le fichier ne doit pas passer pour « traité OK »."""
    from conftest import e2e_bytes
    racine = tmp_path / "entree"
    ecrire(racine / "CentreA" / PID_E2E / f"{PID_E2E}_OD.e2e",
           e2e_bytes(SERIES_MIXTES, pixels=True, oct_illisible=True))
    data = tmp_path / "data.csv"
    data.write_text(f"id;centre;img_oct\n{PID_E2E};CentreA;oui\n", encoding="utf-8")
    sortie = tmp_path / "sortie"
    assert pf.main([str(racine), "--data", str(data), "--sortie", str(sortie),
                    "--cols-skezia", "img_oct:oct_bscan", "--col-centre", "centre"]) == 0
    (j,) = lire_csv(sortie / "rapports" / "journal_images.csv")
    assert j["statut"] == "TRAITEE_QUALITE_LIMITEE", j
    assert "oct_bscan" in j["motif"] and "absentes des sorties" in j["motif"]


def test_series_manquantes_unitaire():
    c = [dict(modalite="oct_bscan"), dict(modalite="autofluorescence"), dict(modalite="infrarouge")]
    assert pf.series_manquantes(c, ["oct_bscan", "autofluorescence"]) == []
    assert pf.series_manquantes(c, ["autofluorescence"]) == ["oct_bscan"]
    assert pf.series_manquantes([dict(modalite="fundus")], ["fundus_color"]) == []
    assert pf.series_manquantes([dict(modalite="autofluorescence")], ["fundus_macular"]) == ["autofluorescence"]
