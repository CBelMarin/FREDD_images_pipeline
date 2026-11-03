"""Métadonnées d'intérêt conservées dans les DICOM produits (vrai code de
correctifs FREDD) : dates, appareil, taille de pixel
recalculée, ordre et série des B-scans, angle seulement s'il est mesuré, UID
d'étude remappé (jamais celui d'origine)."""
import io

import pydicom
import pytest

import pipeline_fredd as pf
from conftest import dicom_bytes, e2e_bytes, ecrire, lire_csv

pytest.importorskip("cv2")

PID = "a1b2c3d4-0000-0000-0000-000000000001"
UID_ETUDE_SOURCE = "1.2.3.4.5.6.7.8.9.12345"


def _dicom(seed, angle=None):
    ds = pydicom.dcmread(io.BytesIO(dicom_bytes(seed=seed)))
    ds.StudyInstanceUID = UID_ETUDE_SOURCE
    ds.StudyDate = "20240312"
    ds.AcquisitionDateTime = "20240312101500"
    ds.ImageLaterality = "R" if seed == 1 else "L"
    ds.Manufacturer, ds.ManufacturerModelName = "Carl Zeiss Meditec", "CLARUS 500"
    ds.PixelSpacing = [0.008, 0.008]
    if angle:
        ds.CameraAngleOfView = angle
    buf = io.BytesIO()
    ds.save_as(buf, enforce_file_format=True)
    return buf.getvalue()


@pytest.fixture(scope="module")
def sorties(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("meta")
    racine = tmp / "entree" / "CentreA" / PID
    ecrire(racine / f"{PID}_OD.dcm", _dicom(1))
    ecrire(racine / f"{PID}_OG.dcm", _dicom(2, angle=45))
    ecrire(racine / f"{PID}_vol.e2e", e2e_bytes(
        [dict(serie=1, motif="OCT ART Volume", lat="R", bscans=[(0, 0, 10, 0), (0, 1, 10, 1), (0, 2, 10, 2)],
              n_oct=3, n_fond=1)], pixels=True))
    data = tmp / "d.csv"
    data.write_text(f"id;centre;img_oct\n{PID};CentreA;oui\n", encoding="utf-8")
    sortie = tmp / "sortie"
    assert pf.main([str(tmp / "entree"), "--data", str(data), "--sortie", str(sortie),
                    "--cols-skezia", "img_oct:oct_bscan", "--col-centre", "centre"]) == 0
    res = {}
    for s in lire_csv(sortie / "rapports" / "sorties_traitees.csv"):
        res.setdefault(s["chemin_source"].rsplit("_", 1)[-1], []).append(
            pydicom.dcmread(str(sortie / s["fichier_sortie"])))
    return res, sortie


def test_dates_et_appareil(sorties):
    res, _ = sorties
    (od,) = res["OD.dcm"]
    assert od.StudyDate == "20240312" and od.AcquisitionDateTime == "20240312101500"
    assert od.ManufacturerModelName == "CLARUS 500"
    for b in res["vol.e2e"]:
        assert b.Manufacturer == "Heidelberg Engineering"
        assert b.AcquisitionDateTime.startswith("2023") and b.StudyDate.startswith("2023")


def test_taille_pixel_recalculee(sorties):
    res, _ = sorties
    (od,) = res["OD.dcm"]
    # 4x4 px source à 0,008 mm, agrandie en 2048x2048 : 0,008 * 4 / 2048 mm
    assert od.PixelSpacing[0] == pytest.approx(0.008 * 4 / 2048, rel=1e-3)
    for b in res["vol.e2e"]:
        assert "PixelSpacing" not in b      # E2E : valeur latérale non fiable, rien plutôt que faux


def test_angle_seulement_si_mesure(sorties):
    res, _ = sorties
    assert "CameraAngleOfView" not in res["OD.dcm"][0]
    assert float(res["OG.dcm"][0].CameraAngleOfView) == 45.0


def test_bscans_ordonnes_dans_une_serie(sorties):
    res, _ = sorties
    vol = sorted(res["vol.e2e"], key=lambda d: int(d.InstanceNumber))
    assert [int(d.InstanceNumber) for d in vol] == [1, 2, 3]
    assert len({d.SeriesInstanceUID for d in vol}) == 1
    assert vol[1].ImageComments.startswith("B-scan 2/3")
    assert all(d.ImageLaterality == "R" for d in vol)


def test_uid_etude_remappe_et_partage(sorties):
    res, sortie = sorties
    od, og = res["OD.dcm"][0], res["OG.dcm"][0]
    assert od.StudyInstanceUID == og.StudyInstanceUID           # même examen -> même UID
    assert od.StudyInstanceUID != UID_ETUDE_SOURCE
    for f in (sortie / "images_traitees").rglob("*.dcm"):
        assert UID_ETUDE_SOURCE.encode() not in f.read_bytes()  # l'UID d'origine ne fuit pas


def test_pseudonymisation_toujours_ok(sorties):
    _, sortie = sorties
    s = lire_csv(sortie / "rapports" / "sorties_traitees.csv")
    assert s and all(x["pseudonymisation"] == "OK" for x in s)


def test_evaluation_qualite_ecrite_dans_image_comments(sorties):
    res, sortie = sorties
    lignes = lire_csv(sortie / "rapports" / "sorties_traitees.csv")
    assert lignes
    attendu = {"OUI": "OK", "QUALITÉ LIMITÉE": "LIMITEE", "INCONNU": "INCONNU"}
    for l in lignes:
        ds = pydicom.dcmread(str(sortie / l["fichier_sortie"]))
        assert f"QUALITE_TECHNIQUE={attendu[l['acceptee']]}" in str(ds.ImageComments)
        assert str(ds.ImageComments).isascii()
    for ds in res["vol.e2e"]:                                   # le début du commentaire OCT est conservé
        assert str(ds.ImageComments).startswith("B-scan ")


def test_orientation_toujours_lisible_apres_ajout_de_la_qualite(sorties):
    _, sortie = sorties
    for l in lire_csv(sortie / "rapports" / "sorties_traitees.csv"):
        if l["image_type"] == "oct_bscan":
            assert l["orientation_coupe"] == pf.orientation_coupe_dicom(sortie / l["fichier_sortie"])
            assert ";" not in l["orientation_coupe"]


def test_commentaire_qualite_ascii_et_sans_point_virgule_dans_les_problemes():
    txt = pf.commentaire_qualite(False, "0,5249", "Cils détectés en haut ; Image floue (netteté)")
    assert txt.startswith("QUALITE_TECHNIQUE=LIMITEE ; score=0.52 ; problemes=")
    assert txt.isascii() and txt.count(";") == 2
    assert pf.commentaire_qualite(True, "", "") == "QUALITE_TECHNIQUE=OK"
    assert pf.commentaire_qualite(None, "x", None) == "QUALITE_TECHNIQUE=INCONNU"
    assert len(pf.commentaire_qualite(False, 0.1, "a" * 5000)) < 400


def test_annoter_qualite_idempotent_et_conserve_le_commentaire(tmp_path):
    from conftest import dicom_bytes as db
    p = tmp_path / "x.dcm"
    ecrire(p, db(comments="B-scan 3/49 ; orientation : horizontale", modality="OPT", rgb=False))
    assert pf.annoter_qualite_dicom(p, True, 0.9, "") is True
    assert pf.annoter_qualite_dicom(p, False, 0.3, "Image trop sombre") is True      # remplace la précédente
    txt = str(pydicom.dcmread(str(p)).ImageComments)
    assert txt.count("QUALITE_TECHNIQUE=") == 1 and "LIMITEE" in txt
    assert txt.startswith("B-scan 3/49 ; orientation : horizontale ; QUALITE_TECHNIQUE")
    assert pf.orientation_coupe_dicom(p) == "horizontale"
    assert pf.annoter_qualite_dicom(tmp_path / "absent.dcm", True, 1, "") is False


def test_mesures_taille_et_pixels_utiles_dans_le_rapport(sorties):
    _, sortie = sorties
    lignes = lire_csv(sortie / "rapports" / "sorties_traitees.csv")
    for h in ("largeur_px", "hauteur_px", "pct_pixels_utiles"):
        assert h in pf.SORTIES_HEADERS
    for l in lignes:
        ds = pydicom.dcmread(str(sortie / l["fichier_sortie"]))
        assert int(l["largeur_px"]) == ds.Columns and int(l["hauteur_px"]) == ds.Rows
        assert 0.0 <= float(l["pct_pixels_utiles"]) <= 100.0


def test_mesures_image_noire_et_image_claire(tmp_path):
    import numpy as np
    from pydicom.uid import ExplicitVRLittleEndian
    for valeur, attendu in ((0, 0.0), (200, 100.0)):
        ds = pydicom.dcmread(io.BytesIO(dicom_bytes(rgb=False)))
        ds.Rows = ds.Columns = 8
        ds.PixelData = np.full((8, 8), valeur, dtype=np.uint8).tobytes()
        ds.file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
        p = tmp_path / f"v{valeur}.dcm"
        ds.save_as(str(p), enforce_file_format=True)
        assert pf.mesures_image_dicom(p) == (8, 8, f"{attendu:.1f}")
    assert pf.mesures_image_dicom(tmp_path / "absent.dcm") == ("", "", "")
