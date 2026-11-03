"""Montage Zeiss CLARUS reconstruit (UWF) : repérage, exclusion si les natives sont là, dates anonymisées.
Structure des métadonnées relevée sur UN examen réel ; jeu synthétique."""
import io

import pydicom
from pydicom.dataset import Dataset

import analyse_contenu as ac
import pipeline_fredd as pf
from conftest import dicom_bytes


def _source(uid, code="121329"):
    it = Dataset()
    it.ReferencedSOPClassUID = "1.2.840.10008.5.1.4.1.1.77.1.5.1"
    it.ReferencedSOPInstanceUID = uid
    c = Dataset()
    c.CodeValue, c.CodingSchemeDesignator, c.CodeMeaning = code, "DCM", "Source image for montage"
    it.PurposeOfReferenceCodeSequence = [c]
    return it


def _ds(image_type, sources=(), uid="1.2.3.1"):
    ds = pydicom.dcmread(io.BytesIO(dicom_bytes()))
    ds.ImageType = image_type
    ds.ImageLaterality = "R"
    ds.SOPInstanceUID = uid
    ds.Manufacturer, ds.ManufacturerModelName = "Carl Zeiss Meditec", "CLARUS 500"
    if sources:
        ds.SourceImageSequence = [_source(u) for u in sources]
    return ds


def _ecrire(tmp_path, nom, ds):
    p = tmp_path / nom
    ds.save_as(str(p))
    return p


def test_montage_repere_et_capture_simple_non(tmp_path):
    simple = _ds(["DERIVED", "PRIMARY", "CORRECTED", "COLOR", "UWF"], ["9.9.9"], uid="1.1")
    montage = _ds(["DERIVED", "PRIMARY", "MONTAGE", "COLOR", "UWF"], ["1.1", "1.2"], uid="1.3")
    rs = ac.analyser(_ecrire(tmp_path, "s.dcm", simple), "DICOM")[0]
    rm = ac.analyser(_ecrire(tmp_path, "m.dcm", montage), "DICOM")[0]
    assert not rs["montage"] and rs["sop_uid"] == "1.1"       # « UWF » seul ne suffit pas
    assert rm["montage"] and rm["sources_montage"] == ["1.1", "1.2"] and "montage reconstruit" in rm["detail"]


def test_deux_sources_sans_mot_montage_suffisent(tmp_path):
    ds = _ds(["DERIVED", "PRIMARY", "CORRECTED", "COLOR", "UWF"], ["1.1", "1.2"])
    assert ac.analyser(_ecrire(tmp_path, "m.dcm", ds), "DICOM")[0]["montage"]


def _rec(statut, contenu, pid="P1"):
    return {"statut": statut, "id_fredd": pid, "categorie": "DICOM", "_contenu": [contenu],
            "contenu_detecte": "", "motif": ""}


def _c(uid, montage=False, sources=()):
    return ac.ligne("fundus", "OD", sop_uid=uid, montage=montage, sources_montage=sources)


def test_montage_ecarte_si_toutes_les_natives_sont_la():
    n1, n2 = _rec("A_TRAITER", _c("1.1")), _rec("A_TRAITER", _c("1.2"))
    m = _rec("A_TRAITER", _c("1.3", True, ["1.1", "1.2"]))
    pf.marquer_montages([n1, n2, m])
    assert m["statut"] == "NON_TRAITEE_MONTAGE" and n1["statut"] == n2["statut"] == "A_TRAITER"
    assert "composante_montage" in n1["_contenu"][0]["detail"] and "composante" in n2["contenu_detecte"]


def test_montage_conserve_si_une_native_manque_ou_autre_patient():
    n1 = _rec("A_TRAITER", _c("1.1"))
    m = _rec("A_TRAITER", _c("1.3", True, ["1.1", "1.2"]))
    pf.marquer_montages([n1, m])
    assert m["statut"] == "A_TRAITER" and "source absente" in m["_contenu"][0]["alerte"]
    autre = _rec("A_TRAITER", _c("1.2"), pid="P2")           # même UID mais autre patient : ne compte pas
    m2 = _rec("A_TRAITER", _c("1.3", True, ["1.1", "1.2"]))
    pf.marquer_montages([n1, autre, m2])
    assert m2["statut"] == "A_TRAITER"


def test_native_deja_ecartee_ne_compte_pas():
    n1, n2 = _rec("A_TRAITER", _c("1.1")), _rec("MISE_DE_COTE_ID_AMBIGU", _c("1.2"))
    m = _rec("A_TRAITER", _c("1.3", True, ["1.1", "1.2"]))
    pf.marquer_montages([n1, n2, m])
    assert m["statut"] == "A_TRAITER"


def test_champ_du_montage_dans_le_lecteur(tmp_path):
    from pipeline_wp2 import reader
    ds = _ds(["DERIVED", "PRIMARY", "MONTAGE", "COLOR", "UWF"], ["1.1", "1.2"])
    meta = reader._extract_dicom_metadata(ds)
    assert meta["champ"] == "ultra_grand_champ_reconstruit"
    simple = reader._extract_dicom_metadata(_ds(["DERIVED", "PRIMARY", "CORRECTED", "COLOR", "UWF"], ["1.1"]))
    assert "champ" not in simple


def test_dates_anonymisees_non_recopiees():
    from pipeline_wp2 import reader
    assert reader.date_plausible("20260615113457.088") and not reader.date_plausible("19740203120000")
    assert not reader.date_plausible("19000101") and not reader.date_plausible("")
    ds = _ds(["ORIGINAL", "PRIMARY"])
    ds.StudyDate, ds.AcquisitionDateTime = "19000101", "19740203120000"
    meta = reader._extract_dicom_metadata(ds)
    assert meta["acquisition_datetime"] == "" and meta["study_date"] == "" and meta["date_non_fiable"]
    ds.StudyDate, ds.AcquisitionDateTime = "20260615", "20260615113457.088"
    meta = reader._extract_dicom_metadata(ds)
    assert meta["acquisition_datetime"].startswith("2026") and "date_non_fiable" not in meta


def test_mode_uwf_distingue_au_catalogue_pas_a_la_confrontation(tmp_path):
    import catalogue_images as cat
    from pipeline_wp2 import reader
    uwf = _ds(["DERIVED", "PRIMARY", "CORRECTED", "COLOR", "UWF"], ["1.1"])
    wf = _ds(["DERIVED", "PRIMARY", "CORRECTED", "COLOR", "WF"], ["1.1"])
    mont = _ds(["DERIVED", "PRIMARY", "MONTAGE", "COLOR", "UWF"], ["1.1", "1.2"])
    assert reader._extract_dicom_metadata(uwf)["uwf"] == "natif"
    assert "uwf" not in reader._extract_dicom_metadata(wf)
    assert reader._extract_dicom_metadata(mont)["uwf"] == "montage"
    # Confrontation SKEZIA : même modalité (« fundus ») que l'image soit UWF ou non
    r_uwf = ac.analyser(_ecrire(tmp_path, "u.dcm", uwf), "DICOM")[0]
    r_wf = ac.analyser(_ecrire(tmp_path, "w.dcm", wf), "DICOM")[0]
    assert r_uwf["modalite"] == r_wf["modalite"] == "fundus"
    # Catalogue : colonne mode_uwf lue dans ImageComments
    lot = tmp_path / "C" / "2026-10-22"
    lot.mkdir(parents=True)
    ds = _ds(["ORIGINAL", "PRIMARY"])
    ds.ImageComments = "champ : grand_champ (méthode : coins) ; uwf : natif"
    f = _ecrire(lot, "a.dcm", ds)
    fiche = cat.fiche(f, lot, pct_connu="50")
    assert (fiche["mode_uwf"], fiche["champ"]) == ("natif", "grand_champ")


def test_writer_ecrit_champ_et_uwf(tmp_path):
    import numpy as np
    from pipeline_wp2.writer import write_processed_dicom
    img = np.zeros((8, 8, 3), dtype=np.uint8)
    meta = {"patient_id": "x", "source_file": "a.dcm", "champ": "ultra_grand_champ_reconstruit",
            "champ_methode": "metadonnees_montage", "uwf": "montage", "date_non_fiable": True}
    p = write_processed_dicom(img, meta, "R", None, None, None, tmp_path / "o.dcm")
    txt = str(pydicom.dcmread(str(p)).ImageComments)
    assert "champ : ultra_grand_champ_reconstruit" in txt and "uwf : montage" in txt and "non fiable" in txt
    # Une image sans champ ni uwf ni date douteuse n'a pas de commentaire
    p2 = write_processed_dicom(img, {"patient_id": "x", "source_file": "b.dcm"}, "R", None, None, None, tmp_path / "o2.dcm")
    assert not str(getattr(pydicom.dcmread(str(p2)), "ImageComments", "") or "")
