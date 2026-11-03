"""Visionneuse locale DICOM / E2E."""
import io
import json
import threading
import urllib.request
from http.server import ThreadingHTTPServer

import pydicom

import visionneuse as vz
from conftest import VRAI_IPP, VRAI_NOM, dicom_bytes, e2e_bytes

SERIES_MIXTES = [
    dict(serie=1, motif="Line", lat="R", bscans=[(0, 0, 10, 0)], n_oct=3, n_fond=1),  # OCT + IR de repérage
    dict(serie=2, en_face="IR", lat="R", n_fond=1),
    dict(serie=3, en_face="Autofluorescence", lat="R", n_fond=1),
]


def test_dicom_image_et_identifiants():
    info, series = vz.analyser_fichier("x_OD.dcm", dicom_bytes(seed=1))
    assert info["format"] == "DICOM" and len(series) == 1 and info["series"][0]["n"] == 1
    vals = {i["tag"]: i["valeur"] for i in info["identifiants"]}
    assert vals["PatientName"] == VRAI_NOM and vals["PatientID"] == VRAI_IPP
    assert vals["PatientBirthDate"] == "19700101"
    assert vz.png(series[0]["images"][0])[:8] == b"\x89PNG\r\n\x1a\n"


def test_dicom_multiframe():
    info, series = vz.analyser_fichier("x.dcm", dicom_bytes(frames=5, rgb=False, modality="OPT"))
    assert info["series"][0]["n"] == 5 and info["series"][0]["titre"].startswith("OCT")


def test_dicom_pseudonymise_sans_identifiant():
    ds = pydicom.dcmread(io.BytesIO(dicom_bytes()))
    for kw in ("PatientName", "PatientBirthDate"):
        delattr(ds, kw)
    ds.PatientID = "a1b2c3d4-0000-0000-0000-000000000001"
    buf = io.BytesIO()
    ds.save_as(buf, enforce_file_format=True)
    info, _ = vz.analyser_fichier("sortie.dcm", buf.getvalue())
    assert [i["tag"] for i in info["identifiants"]] == ["PatientID"]


def test_raw_data_signale():
    ds = pydicom.dcmread(io.BytesIO(dicom_bytes()))
    ds.SOPClassUID = ds.file_meta.MediaStorageSOPClassUID = vz.SOP_RAW
    del ds.PixelData
    buf = io.BytesIO()
    ds.save_as(buf, enforce_file_format=True)
    info, series = vz.analyser_fichier("raw.dcm", buf.getvalue())
    assert not series and "Raw Data" in info["pipeline"]


def test_e2e_series_ir_marquees_et_identite():
    info, series = vz.analyser_fichier("x.e2e", e2e_bytes(SERIES_MIXTES, pixels=True))
    assert info["format"].startswith("E2E")
    par_titre = {s["titre"]: s for s in info["series"]}
    assert par_titre["OCT — B-scans"]["n"] == 3 and not par_titre["OCT — B-scans"]["exclue_ir"]
    assert par_titre["IR de repérage de l'OCT"]["exclue_ir"]
    ir_seule = [s for s in info["series"] if s["titre"].startswith("Infrarouge")]
    af = [s for s in info["series"] if s["titre"].startswith("Autofluorescence")]
    assert len(ir_seule) == 1 and ir_seule[0]["exclue_ir"]
    assert len(af) == 1 and not af[0]["exclue_ir"]
    assert "DUPONT" in {i["valeur"] for i in info["identifiants"]}
    assert "exclues" in info["pipeline"]


def test_fichier_illisible():
    info, series = vz.analyser_fichier("x.dcm", b"pas un dicom")
    assert not series and (info.get("erreur") or info["alertes"])


def test_serveur_local():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), vz.Gestionnaire)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        assert b"Visionneuse FREDD" in urllib.request.urlopen(base + "/").read()
        req = urllib.request.Request(base + "/analyser", data=dicom_bytes(), method="POST",
                                     headers={"X-Nom-Fichier": "essai%20OD.dcm"})
        info = json.loads(urllib.request.urlopen(req).read())
        assert info["nom"] == "essai OD.dcm"
        img = urllib.request.urlopen(f"{base}/image?t={info['jeton']}&s=0&i=0")
        assert img.headers["Content-Type"] == "image/png"
    finally:
        srv.shutdown()


def test_orientation_coupe_lue_dans_les_commentaires_du_dicom_traite():
    """Un OCT traité par le pipeline porte « orientation : … » dans ImageComments :
    la visionneuse doit l'afficher (série + commentaires)."""
    info, series = vz.analyser_fichier("sortie.dcm", dicom_bytes(comments="B-scan 3/49 ; orientation : horizontale",
                                                                 modality="OPT", rgb=False))
    assert info["series"][0]["orientation"] == "horizontale"
    assert {"libelle": "Commentaires de l'image", "valeur": "B-scan 3/49 ; orientation : horizontale"} in info["contexte"]


def test_orientation_coupe_absente_sans_commentaire():
    # OCT en DICOM sans orientation lisible => « autre » (et non plus vide)
    info, _ = vz.analyser_fichier("sortie.dcm", dicom_bytes(modality="OPT", rgb=False))
    assert info["series"][0]["orientation"].startswith("autre")
