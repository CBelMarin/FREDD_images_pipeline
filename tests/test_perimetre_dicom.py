"""Périmètre DICOM (décisions FREDD) : FA/ICGA exclues, OCT DICOM = « autre »,
niveaux de gris = AF présumée (un fond d'œil FREDD est en couleur)."""
import io

import pydicom

import analyse_contenu as ac
import pipeline_fredd as pf
from conftest import dicom_bytes


def _ecrire(tmp_path, nom, **champs):
    ds = pydicom.dcmread(io.BytesIO(dicom_bytes(rgb=champs.pop("rgb", True), modality=champs.pop("modality", "OP"))))
    for k, v in champs.items():
        setattr(ds, k, v)
    p = tmp_path / nom
    ds.save_as(str(p))
    return p


def _rec(cont, categorie="DICOM"):
    return {"statut": "A_TRAITER", "id_fredd": "P1", "categorie": categorie, "_contenu": cont,
            "contenu_detecte": "", "motif": ""}


def test_angiographies_exclues_dicom(tmp_path):
    for nom, desc, mod in (("fa.dcm", "Fluorescein Angiography", "angiographie_fluo"), ("icg.dcm", "ICGA late", "icga")):
        r = ac.analyser(_ecrire(tmp_path, nom, SeriesDescription=desc), "DICOM")[0]
        assert r["modalite"] == mod
        rec = _rec([r])
        pf.marquer_raw_data([rec])
        assert rec["statut"] == "NON_TRAITEE_ANGIO"


def test_fond_couleur_et_oct_non_exclus(tmp_path):
    rec = _rec(ac.analyser(_ecrire(tmp_path, "c.dcm"), "DICOM"))
    pf.marquer_raw_data([rec])
    assert rec["statut"] == "A_TRAITER"


def test_e2e_angio_seule_ou_avec_ir_exclue_mais_pas_avec_oct():
    angio, ir, oct_ = (ac.ligne(m) for m in ("angiographie_fluo", "infrarouge", "oct_bscan"))
    assert pf._que_angio([angio, ir]) and not pf._que_angio([angio, oct_]) and not pf._que_angio([ir])
    assert pf.series_manquantes([angio], []) == []   # une angio exclue n'est pas une « série manquante »


def test_oct_dicom_va_dans_autre(tmp_path):
    p = _ecrire(tmp_path, "o.dcm", modality="OPT", SOPClassUID="1.2.840.10008.5.1.4.1.1.77.1.5.4")
    r = ac.analyser(p, "DICOM")[0]
    assert r["modalite"] == "oct_bscan" and "autre" in r["orientation"]
    assert "horizontale" not in r["orientation"] and "verticale" not in r["orientation"]


def test_niveaux_de_gris_dicom_sont_une_af_presumee(tmp_path):
    p = _ecrire(tmp_path, "g.dcm", rgb=False)
    r = ac.analyser(p, "DICOM")[0]
    assert r["modalite"] == "autofluorescence" and "AF présumée" in r["alerte"]
    from pipeline_wp2 import reader
    assert reader._classify_dicom_image(pydicom.dcmread(str(p))) == "autofluorescence"


def test_reader_dicom_gris_presume_et_angio_vide(tmp_path):
    from pipeline_wp2 import reader
    imgs = reader.read_dicom(_ecrire(tmp_path, "g.dcm", rgb=False))
    assert imgs and imgs[0].image_type == "autofluorescence" and imgs[0].metadata.get("af_presumee")
    assert reader.read_dicom(_ecrire(tmp_path, "fa.dcm", SeriesDescription="Fluorescein Angiography")) == []
    af = reader.read_dicom(_ecrire(tmp_path, "af.dcm", ImageType=["ORIGINAL", "PRIMARY", "FAF"], rgb=False))
    assert af and not af[0].metadata.get("af_presumee")   # mention d'AF explicite : pas « présumée »
