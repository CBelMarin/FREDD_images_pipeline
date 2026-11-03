"""Extraction des métadonnées : modalité, œil, orientation OCT, nombre de
B-scans — DICOM et E2E, formats variés et fichiers abîmés.

Les E2E sont SYNTHÉTIQUES (voir conftest.e2e_bytes) : ces tests vérifient la
logique d'interprétation, pas la conformité aux vrais fichiers Heidelberg
(=> recette.py sur un échantillon réel).
"""
import io
import zipfile

import pydicom
import pytest

import analyse_contenu as ac
from conftest import dicom_bytes, e2e_bytes

SOP_OP = "1.2.840.10008.5.1.4.1.1.77.1.5.1"
SOP_OCT = "1.2.840.10008.5.1.4.1.1.77.1.5.4"
SOP_RAW = "1.2.840.10008.5.1.4.1.1.66"


def _dicom(tmp_path, nom="x.dcm", **tags):
    ds = pydicom.dcmread(io.BytesIO(dicom_bytes(frames=tags.pop("frames", 1))))
    sop = tags.pop("sop", None)
    if sop:
        ds.SOPClassUID = sop
        ds.file_meta.MediaStorageSOPClassUID = sop
    for k, v in tags.items():
        if v is None:
            if k in ds:
                delattr(ds, k)
        else:
            setattr(ds, k, v)
    p = tmp_path / nom
    ds.save_as(str(p))
    return p


# ═════════════ DICOM ═════════════

@pytest.mark.parametrize("tags,nom,attendu", [
    # (modalite, oeil, source_oeil, alerte?)
    (dict(ImageLaterality="R"), "x.dcm", ("fundus", "OD", "metadonnees", False)),
    (dict(ImageLaterality="L"), "x.dcm", ("fundus", "OG", "metadonnees", False)),
    (dict(ImageLaterality="L"), "300b72a1_OD.dcm", ("fundus", "OG", "metadonnees", True)),   # nom ≠ DICOM
    (dict(ImageLaterality="R"), "300b72a1_OD.dcm", ("fundus", "OD", "metadonnees", False)),
    (dict(ImageLaterality=None, Laterality="L"), "x.dcm", ("fundus", "OG", "metadonnees", False)),  # niveau série
    (dict(ImageLaterality="B"), "300b72a1_OG.dcm", ("fundus", "OG", "nom_fichier", False)),  # 'B' = deux yeux
    (dict(ImageLaterality="U"), "x.dcm", ("fundus", "", "", False)),                          # inconnu
    (dict(ImageLaterality=None), "photo_OS.dcm", ("fundus", "OG", "nom_fichier", False)),     # OS = œil gauche
    (dict(Modality="OPT", sop=SOP_OCT, ImageLaterality="R"), "x.dcm", ("oct_bscan", "OD", "metadonnees", False)),
    (dict(Modality="OT", sop=SOP_OCT), "x.dcm", ("oct_bscan", "", "", False)),               # SOP prime sur Modality
    (dict(Modality="OCT"), "x.dcm", ("oct_bscan", "", "", False)),
    (dict(ImageType=["ORIGINAL", "PRIMARY", "FAF"]), "x.dcm", ("autofluorescence", "", "", False)),
    (dict(SeriesDescription="Autofluorescence 488"), "x.dcm", ("autofluorescence", "", "", False)),
    (dict(SeriesDescription="BluePeak"), "x.dcm", ("autofluorescence", "", "", False)),
    (dict(SeriesDescription="Fluorescein Angiography"), "x.dcm", ("angiographie_fluo", "", "", False)),
    (dict(ImageType=["ORIGINAL", "PRIMARY", "FA"]), "x.dcm", ("angiographie_fluo", "", "", False)),
    (dict(SeriesDescription="ICGA early"), "x.dcm", ("icga", "", "", False)),
    (dict(Modality="OT", sop=SOP_RAW), "x.dcm", ("raw_data", "", "", False)),
    # cas réel Zeiss (lot 09oct26) : Raw Data avec Modality=OP -> pas un fond d'œil
    (dict(Modality="OP", sop=SOP_RAW, ImageLaterality="R"), "x.dcm", ("raw_data", "OD", "metadonnees", False)),
    (dict(Modality="SR", sop="1.2.840.10008.5.1.4.1.1.88.11"), "x.dcm", ("inconnu", "", "", False)),
    # faux positifs à éviter
    (dict(SeriesDescription="Fundus color Clarus"), "x.dcm", ("fundus", "", "", False)),
    (dict(SeriesDescription="Safari"), "x.dcm", ("fundus", "", "", False)),       # 'af' dans un mot
    (dict(SeriesDescription="FAFNIR"), "x.dcm", ("fundus", "", "", False)),       # 'FAF' dans un mot
])
def test_dicom_modalite_oeil(tmp_path, tags, nom, attendu):
    r = ac.analyser(_dicom(tmp_path, nom, **tags), "DICOM", str(tmp_path / nom))[0]
    assert (r["modalite"], r["lateralite"], r["source_oeil"], bool(r["alerte"])) == attendu, r


def test_dicom_oct_multiframe_nb_bscans(tmp_path):
    r = ac.analyser(_dicom(tmp_path, Modality="OPT", sop=SOP_OCT, frames=25), "DICOM")[0]
    assert (r["modalite"], r["nb_bscans"]) == ("oct_bscan", "25")


def test_dicom_appareil(tmp_path):
    r = ac.analyser(_dicom(tmp_path, Manufacturer="Carl Zeiss Meditec", ManufacturerModelName="CLARUS 700"),
                    "DICOM")[0]
    assert r["appareil"] == "Carl Zeiss Meditec CLARUS 700"


def test_dicom_identite_jamais_recopiee(tmp_path):
    r = ac.analyser(_dicom(tmp_path, PatientName="DUPONT^JEAN", PatientID="IPP42", SeriesDescription="FAF"), "DICOM")
    assert "DUPONT" not in repr(r) and "IPP42" not in repr(r)


@pytest.mark.parametrize("donnees", [b"", b"DICM", b"\0" * 128 + b"DICM" + b"\x02\x00", dicom_bytes()[:200]])
def test_dicom_abime_ne_plante_pas(tmp_path, donnees):
    p = tmp_path / "300b72a1_OD.dcm"
    p.write_bytes(donnees)
    r = ac.analyser(p, "DICOM", str(p))[0]
    # soit illisible (erreur renseignée), soit lu partiellement ; jamais d'exception
    assert r["erreur"] or r["modalite"]
    assert r["lateralite"] == "OD"   # l'œil du nom reste disponible


# ═════════════ E2E ═════════════

def _e2e(tmp_path, series, **kw):
    p = tmp_path / "x.e2e"
    p.write_bytes(e2e_bytes(series=series, **kw))
    return ac.analyser_e2e(p)


H = [(0, 5, 20, 5)]
V = [(10, 0, 10, 20)]


@pytest.mark.parametrize("serie,attendu", [
    # (modalite, oeil, orientation, nb_bscans)
    (dict(motif="OCT Line", lat="R", bscans=H, n_oct=1), ("oct_bscan", "OD", "horizontale", 1)),
    (dict(motif="OCT Line", lat="L", bscans=V, n_oct=1), ("oct_bscan", "OG", "verticale", 1)),
    (dict(motif="OCT Line", lat="R", lat_bloc=3, bscans=H, n_oct=1), ("oct_bscan", "OD", "horizontale", 1)),
    (dict(motif="Volume", lat="R", bscans=[(0, i, 20, i) for i in range(49)], n_oct=49),
     ("oct_bscan", "OD", "volume, lignes horizontales", 49)),
    (dict(motif="Volume", lat="L", bscans=[(i, 0, i, 20) for i in range(19)], n_oct=19),
     ("oct_bscan", "OG", "volume, lignes verticales", 19)),
    (dict(motif="Star", lat="R", bscans=[(10 - 10 * c, 10 - 10 * s, 10 + 10 * c, 10 + 10 * s) for c, s in
                                          ((1, 0), (0.866, 0.5), (0.5, 0.866), (0, 1), (-0.5, 0.866), (-0.866, 0.5))],
          n_oct=6), ("oct_bscan", "OD", "radiale/mixte", 6)),
    (dict(motif="Line", lat="R", bscans=[(0, 0, 20, 3)], n_oct=1), ("oct_bscan", "OD", "horizontale", 1)),   # 8,5° : horizontale
    (dict(motif="Line", lat="R", bscans=[(0, 0, 20, 20)], n_oct=1), ("oct_bscan", "OD", "oblique", 1)),
    # cercle : point dégénéré ET centre cohérent (centrePos == le point lui-même) -> pas de
    # direction. À distinguer du bug d'export réel (centrePos incohérent avec le point dégénéré,
    # voir test_e2e_orientation_bscan_degenere_reconstruit_via_centre dans test_contenu.py).
    (dict(motif="Circle", lat="R", bscans=[(5, 5, 5, 5, 5, 5)], n_oct=1), ("oct_bscan", "OD", "", 1)),
    (dict(motif="OCT", lat="R", bscans=H, n_oct=1, en_face="IR", n_fond=1), ("oct_bscan", "OD", "horizontale", 1)),
    (dict(en_face="Autofluorescence", lat="R", n_fond=1), ("autofluorescence", "OD", "", "")),
    (dict(en_face="BAF", lat="L", n_fond=1), ("autofluorescence", "OG", "", "")),
    (dict(en_face="Blue Peak", lat="L", n_fond=1), ("autofluorescence", "OG", "", "")),
    (dict(en_face="IR", lat="L", n_fond=1), ("infrarouge", "OG", "", "")),
    (dict(en_face="FA", lat="L", n_fond=1), ("angiographie_fluo", "OG", "", "")),
    (dict(en_face="ICGA", lat="L", n_fond=1), ("icga", "OG", "", "")),
    (dict(en_face="MultiColor", lat="L", n_fond=1), ("fundus_multicolor", "OG", "", "")),
    (dict(en_face="XYZ", lat="L", n_fond=1), ("fundus_autre", "OG", "", "")),
    (dict(motif="OCT", bscans=H, n_oct=1), ("oct_bscan", "", "horizontale", 1)),                         # pas d'œil
])
def test_e2e_une_serie(tmp_path, serie, attendu):
    res = _e2e(tmp_path, [dict(serie=1, **serie)])
    assert len(res) == 1
    r = res[0]
    assert (r["modalite"], r["lateralite"], r["orientation"], r["nb_bscans"]) == attendu, r


def test_e2e_plusieurs_series_deux_yeux_deux_etudes(tmp_path):
    res = _e2e(tmp_path, [
        dict(study=1, serie=1, motif="OCT", lat="R", bscans=H, n_oct=1),
        dict(study=1, serie=2, en_face="FAF", lat="R", n_fond=1),
        dict(study=1, serie=3, motif="OCT", lat="L", bscans=V, n_oct=1),
        dict(study=2, serie=1, motif="OCT", lat="L", bscans=H, n_oct=1),   # même n° de série, autre étude
    ])
    vu = sorted((r["serie"], r["modalite"], r["lateralite"], r["orientation"]) for r in res)
    assert vu == [("1_1", "oct_bscan", "OD", "horizontale"), ("1_2", "autofluorescence", "OD", ""),
                  ("1_3", "oct_bscan", "OG", "verticale"), ("2_1", "oct_bscan", "OG", "horizontale")]


def test_e2e_entete_multivolume(tmp_path):
    res = _e2e(tmp_path, [dict(serie=1, motif="OCT", lat="L", bscans=V, n_oct=1)], entete_multivolume=True)
    assert [(r["modalite"], r["lateralite"], r["orientation"]) for r in res] == [("oct_bscan", "OG", "verticale")]


def test_e2e_oeil_contradictoire_alerte(tmp_path):
    p = tmp_path / "300b72a1_OD.e2e"
    p.write_bytes(e2e_bytes([dict(serie=1, motif="OCT", lat="R", bscans=H, n_oct=1), dict(serie=1, lat="L")]))
    r = ac.analyser(p, "E2E_HEIDELBERG", str(p))[0]
    assert r["lateralite"] == "contradictoire" and "contradictoire" in r["alerte"]


def test_e2e_oeil_nom_contredit(tmp_path):
    p = tmp_path / "300b72a1_OG.e2e"
    p.write_bytes(e2e_bytes([dict(serie=1, motif="OCT", lat="R", bscans=H, n_oct=1)]))
    r = ac.analyser(p, "E2E_HEIDELBERG", str(p))[0]
    assert r["lateralite"] == "OD" and "OG" in r["alerte"]


def test_e2e_serie_technique_ignoree(tmp_path):
    """Une série sans image ni libellé (bloc d'œil seul) ne produit pas de ligne."""
    res = _e2e(tmp_path, [dict(serie=1, motif="OCT", lat="R", bscans=H, n_oct=1), dict(serie=9, lat="L")])
    assert [r["serie"] for r in res] == ["1_1"]


@pytest.mark.parametrize("coupe", [0, 10, 36 + 52 + 20, 300, -40])
def test_e2e_tronque_ne_plante_pas(tmp_path, coupe):
    complet = e2e_bytes([dict(serie=1, motif="OCT", lat="R", bscans=H * 3, n_oct=3)])
    p = tmp_path / "x.e2e"
    p.write_bytes(complet[:coupe] if coupe >= 0 else complet[:len(complet) + coupe])
    res = ac.analyser(p, "E2E_HEIDELBERG", "/c/300b72a1_OD.e2e")
    assert res
    for r in res:
        # jamais d'exception ; soit une erreur explicite, soit un contenu ACCOMPAGNÉ d'une alerte de troncature
        assert r["erreur"] or "tronqué" in r["alerte"], r
        assert r["lateralite"] == "OD"


def test_e2e_identite_jamais_lue(tmp_path):
    res = _e2e(tmp_path, [dict(serie=1, motif="OCT", lat="R", bscans=H, n_oct=1)], nom_patient="SECRETNOM")
    assert "SECRETNOM" not in repr(res) and "IPP987654321" not in repr(res) and "JEAN" not in repr(res)


# ═════════════ Images standards ═════════════

@pytest.mark.parametrize("nom,oeil", [("300b72a1_OD.jpg", "OD"), ("300b72a1 OG.png", "OG"), ("x_OS_FO.tif", "OG"),
                                      ("photo.jpg", ""), ("OD_OG.jpg", ""), ("Optos.jpg", ""), ("MODELE.jpg", "")])
def test_image_standard_oeil_depuis_nom(tmp_path, nom, oeil):
    p = tmp_path / nom
    p.write_bytes(b"\xff\xd8\xff" + b"\0" * 10)
    r = ac.analyser(p, "IMAGE_STANDARD", str(p))[0]
    assert (r["modalite"], r["lateralite"]) == ("image_standard", oeil)


# ═════════════ Dans des zips (inventaire autonome) ═════════════

def test_contenu_lu_dans_zip_imbrique(tmp_path, monkeypatch):
    import sys
    import inventaire_images
    from conftest import ecrire, lire_csv
    interne = io.BytesIO()
    with zipfile.ZipFile(interne, "w") as z:
        z.writestr("300b72a1_OG.e2e", e2e_bytes([dict(serie=1, motif="OCT", lat="L", bscans=V, n_oct=1)]))
    externe = io.BytesIO()
    with zipfile.ZipFile(externe, "w") as z:
        z.writestr("300b72a1/examen.zip", interne.getvalue())
    ecrire(tmp_path / "img" / "Nantes.zip", externe.getvalue())
    monkeypatch.setattr(sys, "argv", ["inventaire_images.py", str(tmp_path / "img"), "--sortie", str(tmp_path / "s"),
                                      "--analyse-contenu"])
    inventaire_images.main()
    l = lire_csv(next((tmp_path / "s").glob("contenu_images_*.csv")))
    assert [(x["modalite"], x["lateralite"], x["orientation"]) for x in l] == [("oct_bscan", "OG", "verticale")]


def test_e2e_tronque_en_fin_de_fichier_alerte(tmp_path):
    """Coupé dans le dernier bloc : les métadonnées restent lisibles, mais la
    troncature (copie incomplète) DOIT être signalée."""
    complet = e2e_bytes([dict(serie=1, motif="OCT", lat="R", bscans=H * 3, n_oct=3)])
    p = tmp_path / "x.e2e"
    p.write_bytes(complet[:-5])
    r = ac.analyser(p, "E2E_HEIDELBERG", str(p))[0]
    assert r["modalite"] == "oct_bscan" and "tronqué" in r["alerte"]
    p.write_bytes(complet)
    assert ac.analyser(p, "E2E_HEIDELBERG", str(p))[0]["alerte"] == ""


def test_alertes_cumulees(tmp_path):
    complet = e2e_bytes([dict(serie=1, motif="OCT", lat="R", bscans=H, n_oct=1)])
    p = tmp_path / "300b72a1_OG.e2e"
    p.write_bytes(complet[:-5])
    a = ac.analyser(p, "E2E_HEIDELBERG", str(p))[0]["alerte"]
    assert "tronqué" in a and "OG" in a
