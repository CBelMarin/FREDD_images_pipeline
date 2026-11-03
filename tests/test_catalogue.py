"""Catalogue des images traitées (catalogue_images.py) : une fiche par DICOM, sans chemin source,
reconstructible, exporté avec les images."""
import csv
import zipfile

import pydicom
import pytest

import catalogue_images as cat
import export_images as ex
import fredd
import pipeline_fredd as pf
from conftest import dicom_bytes, ecrire, lire_csv
from test_metadonnees_sorties import sorties  # noqa: F401  (fixture : vrai passage du pipeline)

pytest.importorskip("cv2")


def _catalogues(sortie):
    return sorted((sortie / "images_traitees").rglob(cat.NOM))


def test_un_catalogue_par_lot_apres_un_passage(sorties):
    res, sortie = sorties
    cats = _catalogues(sortie)
    assert len(cats) == 1
    lignes = lire_csv(cats[0])
    sorties_csv = lire_csv(sortie / "rapports" / "sorties_traitees.csv")
    assert len(lignes) == len(sorties_csv) == len(list((sortie / "images_traitees").rglob("*.dcm")))
    assert list(lignes[0]) == cat.ENTETE
    assert {l["centre"] for l in lignes} == {"CentreA"}
    assert all(l["qualite"] in ("OK", "LIMITEE", "INCONNU") for l in lignes)
    attendu = {"OUI": "OK", "QUALITÉ LIMITÉE": "LIMITEE", "INCONNU": "INCONNU"}
    for s in sorties_csv:
        f = s["fichier_sortie"].split("/")[-1].split("\\")[-1]
        l = next(x for x in lignes if x["fichier"].endswith(f))
        assert l["qualite"] == attendu[s["acceptee"]]
        assert l["largeur_px"] == s["largeur_px"] and l["pct_pixels_utiles"] == s["pct_pixels_utiles"]
        assert l["image_type"] == s["image_type"]


def test_le_catalogue_ne_contient_aucun_chemin_source(sorties):
    _, sortie = sorties
    texte = _catalogues(sortie)[0].read_text(encoding="utf-8-sig")
    assert "chemin_source" not in texte and "entree" not in texte and "_OD.dcm" not in texte
    sources = {s["chemin_source"] for s in lire_csv(sortie / "rapports" / "sorties_traitees.csv")}
    assert not any(src in texte for src in sources)


def test_reconstruction_identique_et_images_anciennes_non_evaluees(sorties):
    _, sortie = sorties
    avant = _catalogues(sortie)[0].read_text(encoding="utf-8-sig")
    assert cat.construire(sortie, log=lambda *_: None)
    assert _catalogues(sortie)[0].read_text(encoding="utf-8-sig") == avant
    lot = _catalogues(sortie)[0].parent
    ancienne = lot / "ancien" / "ancienne.dcm"                      # image d'une version sans mention qualité
    ecrire(ancienne, dicom_bytes(comments="B-scan 1/3 ; orientation : horizontale", modality="OPT", rgb=False))
    try:
        bilan = cat.construire(sortie, log=lambda *_: None)
        lignes = lire_csv(lot / cat.NOM)
        l = next(x for x in lignes if x["fichier"].endswith("ancienne.dcm"))
        assert l["qualite"] == "NON_EVALUEE" and l["orientation_coupe"] == "horizontale"
        assert sum(bilan[("CentreA", lot.name)].values()) == len(lignes)
    finally:
        ancienne.unlink()
        ancienne.parent.rmdir()
        cat.construire(sortie, log=lambda *_: None)


def test_a_verifier_en_tete_du_fichier(tmp_path):
    sortie = tmp_path / "sortie"
    lot = sortie / "images_traitees" / "C" / "2026-10-18"
    for i, (flag, ok) in enumerate([("OK", True), ("LIMITEE", False), ("OK", True)]):
        p = lot / f"p{i}" / f"x{i}.dcm"
        ecrire(p, dicom_bytes(comments="", rgb=False))
        assert pf.annoter_qualite_dicom(p, ok, 0.5, "Image floue" if not ok else "")
    cat.construire(sortie, log=lambda *_: None)
    lignes = lire_csv(lot / cat.NOM)
    assert [l["qualite"] for l in lignes] == ["LIMITEE", "OK", "OK"]
    assert lignes[0]["problemes"] == "Image floue"


def test_commande_catalogue_de_fredd(sorties, monkeypatch, capsys):
    _, sortie = sorties
    monkeypatch.setattr(fredd, "lire_config", lambda *_: {"sortie": sortie})
    assert fredd.catalogue() == 0
    assert "Catalogue des images" in capsys.readouterr().out
    monkeypatch.setattr(fredd, "lire_config", lambda *_: {"sortie": sortie / "absent"})
    assert fredd.catalogue() == 2


def test_export_joint_le_catalogue_aux_nouveaux_lots_seulement(tmp_path):
    sortie = tmp_path / "sortie"
    lot = sortie / "images_traitees" / "C" / "2026-10-18"
    for i in range(2):
        ecrire(lot / f"p{i}" / f"x{i}.dcm", dicom_bytes(rgb=False, seed=i))
    cat.construire(sortie, log=lambda *_: None)
    assert ex.exporter(sortie, log=lambda *_: None) == 0
    man = lire_csv(sortie / "export" / "manifest_export.csv")
    with zipfile.ZipFile(sortie / "export" / man[0]["archive"]) as zf:
        assert "C/2026-10-18/" + cat.NOM in zf.namelist()
    ex.exporter(sortie, log=lambda *_: None)                       # rien de nouveau : pas de nouvelle archive
    assert len(lire_csv(sortie / "export" / "manifest_export.csv")) == 1
    ecrire(lot / "p9" / "x9.dcm", dicom_bytes(rgb=False, seed=9))
    cat.construire(sortie, log=lambda *_: None)
    ex.exporter(sortie, log=lambda *_: None)
    man = lire_csv(sortie / "export" / "manifest_export.csv")
    assert len(man) == 2
    with zipfile.ZipFile(sortie / "export" / man[1]["archive"]) as zf:
        noms = zf.namelist()
    assert "C/2026-10-18/" + cat.NOM in noms and sum(n.endswith(".dcm") for n in noms) == 1
