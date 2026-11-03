"""DICOM « Raw Data » (Zeiss) : statut dédié, jamais envoyé à raretia, jamais
compté comme image à la confrontation (constaté sur données réelles)."""
import io

import pydicom

from conftest import FAKE_RARETIA, dicom_bytes, ecrire, lire_csv
import pipeline_fredd as pf

PID = "a1b2c3d4-0000-0000-0000-000000000001"
PID2 = "b2c3d4e5-0000-0000-0000-000000000002"


def raw_bytes():
    ds = pydicom.dcmread(io.BytesIO(dicom_bytes(seed=9)))
    ds.SOPClassUID = ds.file_meta.MediaStorageSOPClassUID = "1.2.840.10008.5.1.4.1.1.66"
    ds.Modality = "OP"  # comme les fichiers Zeiss réels
    buf = io.BytesIO()
    ds.save_as(buf, enforce_file_format=True)
    return buf.getvalue()


def _jeu(tmp_path):
    racine = tmp_path / "entree" / "CentreA"
    ecrire(racine / PID / f"{PID}_OD.dcm", dicom_bytes(seed=1))
    ecrire(racine / PID / f"{PID}_raw.dcm", raw_bytes())
    ecrire(racine / PID2 / f"{PID2}_raw.dcm", raw_bytes())   # patient avec UNIQUEMENT du raw data
    data = tmp_path / "d.csv"
    data.write_text(f"id;centre;img_retino\n{PID};CentreA;oui\n{PID2};CentreA;oui\n", encoding="utf-8")
    return tmp_path / "entree", data


def _lancer(tmp_path, *extra):
    entree, data = _jeu(tmp_path)
    sortie = tmp_path / "sortie"
    rc = pf.main([str(entree), "--data", str(data), "--sortie", str(sortie), "--raretia-path", str(FAKE_RARETIA),
                  "--cols-skezia", "img_retino:fundus", "--col-centre", "centre", *extra])
    assert rc == 0
    return sortie


def test_raw_data_statut_dedie_et_non_envoye(tmp_path):
    sortie = _lancer(tmp_path)
    j = {l["chemin_source"].rsplit("_", 1)[-1] + l["id_fredd"][:2]: l for l in lire_csv(sortie / "rapports" / "journal_images.csv")}
    assert j["OD.dcma1"]["statut"] == "TRAITEE_OK"
    for k in ("raw.dcma1", "raw.dcmb2"):
        assert j[k]["statut"] == "NON_TRAITEE_RAW_DATA"
        assert j[k]["destination"].replace("\\", "/").startswith("non_traitees_format/CentreA/_raw_data/")
    # un seul fichier envoyé à raretia : celui qui contient une image
    assert len(list((sortie / "rapports" / "raretia_bruts").rglob("*.csv"))) == 1
    rows = lire_csv(next((sortie / "rapports" / "raretia_bruts").rglob("*.csv")))
    assert len(rows) == 1


def test_raw_data_pas_compte_comme_image(tmp_path):
    sortie = _lancer(tmp_path)
    conf = {l["id_fredd"]: l for l in lire_csv(sortie / "rapports" / "confrontation_patients.csv")}
    assert conf[PID2]["statut_confrontation"] == "SKEZIA_SEULEMENT"
    assert "fundus" not in conf[PID2]["contenu_trouve"]
    assert "DECLAREE_NON_TROUVEE" in conf[PID2]["detail_par_modalite"]


def test_raw_data_sans_analyse_contenu(tmp_path):
    """Sans l'étape 1b, le raw data part chez raretia, mais l'échec est requalifié."""
    sortie = _lancer(tmp_path, "--pas-analyse-contenu")
    stat = {l["id_fredd"][:2] + l["chemin_source"][-7:]: l["statut"] for l in lire_csv(sortie / "rapports" / "journal_images.csv")}
    assert stat["b2raw.dcm"] == "NON_TRAITEE_RAW_DATA"
