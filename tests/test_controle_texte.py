"""Contrôle du texte incrusté (controle_texte.py) : détecteur simple, sur images synthétiques."""
import io

import numpy as np
import pydicom
import pytest

import controle_texte as ct
import fredd
from conftest import dicom_bytes, ecrire, lire_csv

cv2 = pytest.importorskip("cv2")


def _fond(seed=0, taille=1024):
    rng = np.random.default_rng(seed)
    img = np.zeros((taille, taille, 3), np.uint8)
    cv2.circle(img, (taille // 2, taille // 2), int(taille * 0.46), (40, 90, 200), -1)
    img = cv2.GaussianBlur(img, (0, 0), 25)
    for _ in range(40):
        pts = np.array([[taille // 2 + int(rng.normal(0, 200)), taille // 2 + int(rng.normal(0, 200))] for _ in range(8)])
        cv2.polylines(img, [pts], False, (20, 40, 110), int(rng.integers(2, 5)))
    img = cv2.GaussianBlur(img, (0, 0), 1.5)
    return np.clip(img + rng.normal(0, 6, img.shape), 0, 255).astype(np.uint8)


def _oct(seed=0, h=496, w=768):
    rng = np.random.default_rng(seed)
    img = np.zeros((h, w), np.float32)
    for k, y0 in enumerate(range(150, 330, 22)):
        yy = y0 + 30 * np.sin(np.arange(w) / 120.0)
        for x in range(w):
            img[int(yy[x]):int(yy[x]) + 8, x] += 90 + 40 * (k % 2)
    img = cv2.GaussianBlur(img, (0, 0), 2) * rng.gamma(4, 1 / 4, img.shape)
    return np.clip(img, 0, 255).astype(np.uint8)


def _texte(img, t, pos, echelle=0.9, epaisseur=2):
    out = img.copy()
    cv2.putText(out, t, pos, cv2.FONT_HERSHEY_SIMPLEX, echelle, (255, 255, 255), epaisseur, cv2.LINE_AA)
    return out


def _dcm(path, pixels):
    ds = pydicom.dcmread(io.BytesIO(dicom_bytes(rgb=pixels.ndim == 3)))
    ds.Rows, ds.Columns = pixels.shape[:2]
    ds.PixelData = np.ascontiguousarray(pixels).tobytes()
    buf = io.BytesIO()
    ds.save_as(buf, enforce_file_format=True)
    ecrire(path, buf.getvalue())


def test_images_sans_texte_non_signalees():
    for seed in range(4):
        assert ct.lignes_suspectes(_fond(seed)) == 0
        assert ct.lignes_suspectes(_oct(seed)) == 0
    assert ct.lignes_suspectes(np.zeros((600, 600), np.uint8)) == 0
    assert ct.lignes_suspectes(np.zeros((8, 8), np.uint8)) == 0


def test_images_avec_texte_signalees():
    assert ct.lignes_suspectes(_texte(_fond(), "NOM Prenom 12/03/1980", (20, 40))) >= 1
    assert ct.lignes_suspectes(_texte(_fond(), "ID 4521  18/10/2026", (20, 990), 0.6, 1)) >= 1
    assert ct.lignes_suspectes(_texte(cv2.cvtColor(_oct(), cv2.COLOR_GRAY2BGR), "NOM Prenom OD", (10, 30), 0.7)) >= 1
    grand = cv2.resize(_fond(), (3000, 3000))
    assert ct.lignes_suspectes(_texte(grand, "NOM Prenom 12/03/1980", (60, 120), 2.6, 5)) >= 1


def test_texte_noir_sur_fond_clair_signale():
    clair = np.full((700, 900), 235, np.uint8)
    cv2.putText(clair, "NOM Prenom 12/03/1980", (30, 80), cv2.FONT_HERSHEY_SIMPLEX, 1.0, 0, 2, cv2.LINE_AA)
    assert ct.lignes_suspectes(clair) >= 1
    assert ct.lignes_suspectes(np.full((700, 900), 235, np.uint8)) == 0


def test_deux_lignes_de_texte_comptees():
    img = _texte(_texte(_fond(), "DUPONT Jean", (20, 40)), "12/03/1980  OD", (20, 90))
    assert ct.lignes_suspectes(img) >= 2


@pytest.fixture
def sortie(tmp_path):
    lot = tmp_path / "sortie" / "images_traitees" / "CentreA" / "2026-10-18"
    _dcm(lot / "p1" / "propre.dcm", _fond())
    _dcm(lot / "p2" / "oct.dcm", _oct())
    _dcm(lot / "p3" / "texte.dcm", _texte(_fond(), "NOM Prenom 12/03/1980", (20, 40)))
    return tmp_path / "sortie"


def test_mesure_ne_deplace_rien_et_ne_rapporte_que_les_suspectes(sortie, capsys):
    assert ct.main([str(sortie)]) == 0
    assert len(list((sortie / "images_traitees").rglob("*.dcm"))) == 3
    rap = lire_csv(sortie / "rapports" / ct.NOM_RAPPORT)
    assert [r["fichier"] for r in rap] == ["CentreA/2026-10-18/p3/texte.dcm"] and rap[0]["deplacee"] == "non"
    sortie_texte = capsys.readouterr().out
    assert "CentreA ; 3 ; 1" in sortie_texte and "TOTAL ; 3 ; 1" in sortie_texte and "non déplacées" in sortie_texte


def test_deplacer_puis_restaurer(sortie):
    assert ct.main([str(sortie), "--deplacer"]) == 0
    assert not (sortie / "images_traitees/CentreA/2026-10-18/p3/texte.dcm").exists()
    assert (sortie / ct.DOSSIER_QUARANTAINE / "CentreA/2026-10-18/p3/texte.dcm").is_file()
    assert (sortie / "images_traitees/CentreA/2026-10-18/p1/propre.dcm").is_file()
    assert lire_csv(sortie / "rapports" / ct.NOM_RAPPORT)[0]["deplacee"] == "oui"
    assert ct.main([str(sortie), "--restaurer"]) == 0
    assert (sortie / "images_traitees/CentreA/2026-10-18/p3/texte.dcm").is_file()
    assert not list((sortie / ct.DOSSIER_QUARANTAINE).rglob("*.dcm"))


def test_aucun_identifiant_dans_le_rapport_ni_la_sortie(sortie, capsys):
    ct.main([str(sortie), "--deplacer"])
    texte = (sortie / "rapports" / ct.NOM_RAPPORT).read_text(encoding="utf-8-sig") + capsys.readouterr().out
    assert "Prenom" not in texte and "1980" not in texte


def test_fichier_illisible_compte_sans_planter(sortie, capsys):
    (sortie / "images_traitees/CentreA/2026-10-18/p4").mkdir()
    (sortie / "images_traitees/CentreA/2026-10-18/p4/casse.dcm").write_bytes(b"pas un dicom")
    assert ct.main([str(sortie)]) == 0
    assert "illisibles : 1" in capsys.readouterr().out


def test_usage_et_dossier_absent(tmp_path, capsys):
    assert ct.main([]) == 2
    assert ct.main([str(tmp_path / "vide")]) == 2


def test_commande_texte_de_fredd(sortie, monkeypatch, capsys):
    monkeypatch.setattr(fredd, "lire_config", lambda *_: {"sortie": sortie})
    assert fredd.texte([]) == 0
    assert "Contrôle du texte incrusté" in capsys.readouterr().out


# --- contrôle intégré au pipeline -------------------------------------------------------------------
import pipeline_fredd as pf  # noqa: E402

PID_T = "a1b2c3d4-0000-0000-0000-0000000000aa"


def _passage(tmp_path, pixels, *options):
    racine = tmp_path / "entree" / "CentreA" / PID_T
    _dcm(racine / f"{PID_T}_OD.dcm", pixels)
    data = tmp_path / "d.csv"
    data.write_text(f"id;centre\n{PID_T};CentreA\n", encoding="utf-8")
    sortie = tmp_path / "sortie"
    assert pf.main([str(tmp_path / "entree"), "--data", str(data), "--sortie", str(sortie),
                    "--col-centre", "centre", *options]) == 0
    return sortie


def test_pipeline_met_en_quarantaine_une_image_avec_du_texte(tmp_path):
    sortie = _passage(tmp_path, _texte(_fond(), "NOM Prenom 12/03/1980", (20, 40)))
    ligne = lire_csv(sortie / "rapports" / "journal_images.csv")[0]
    assert ligne["statut"] == "ECHEC_PSEUDONYMISATION" and "texte probablement incrusté" in ligne["motif"]
    assert not list((sortie / "images_traitees").rglob("*.dcm")) if (sortie / "images_traitees").exists() else True
    assert list((sortie / "quarantaine_pseudonymisation").rglob("*.dcm"))
    assert "Prenom" not in ligne["motif"]


def test_pipeline_option_pour_desactiver_le_controle(tmp_path):
    sortie = _passage(tmp_path, _texte(_fond(), "NOM Prenom 12/03/1980", (20, 40)), "--sans-controle-texte")
    ligne = lire_csv(sortie / "rapports" / "journal_images.csv")[0]
    assert ligne["statut"].startswith("TRAITEE_")
    assert list((sortie / "images_traitees").rglob("*.dcm"))


def test_pipeline_image_sans_texte_non_touchee(tmp_path):
    sortie = _passage(tmp_path, _fond())
    assert lire_csv(sortie / "rapports" / "journal_images.csv")[0]["statut"].startswith("TRAITEE_")


def test_controler_sortie_fichier_illisible_par_prudence(tmp_path):
    p = tmp_path / "casse.dcm"
    p.write_bytes(b"pas un dicom")
    ok, motif = ct.controler_sortie(p)
    assert ok is False and "impossible" in motif
