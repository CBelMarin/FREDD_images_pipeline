"""Export en archives ZIP de taille bornée (export_images.py) : découpage, reprise, contrôle."""
import csv
import os
import zipfile
from pathlib import Path

import pytest

import export_images as ex


def _jeu(tmp_path, n=6, taille=100_000, centre="CentreA", date="2026-10-01"):
    sortie = tmp_path / "sortie"
    for i in range(n):
        p = sortie / "images_traitees" / centre / date / f"a1b2c3d{i % 3}" / f"img{i}.dcm"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(os.urandom(taille))        # incompressible : la taille des archives est prévisible
    return sortie


def _lire(path):
    with open(path, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f, delimiter=";"))


def test_decoupage_en_archives_bornees(tmp_path):
    sortie = _jeu(tmp_path)                                  # 6 fichiers de 100 ko
    assert ex.exporter(sortie, taille_max_go=0.0003, log=lambda *_: None) == 0   # ~300 ko : 3-4 fichiers par archive
    man = _lire(sortie / "export" / "manifest_export.csv")
    assert 2 <= len(man) <= 3
    assert sum(int(m["nb_fichiers"]) for m in man) == 6
    for m in man:
        z = sortie / "export" / m["archive"]
        assert z.is_file() and z.stat().st_size == int(m["taille_octets"])
        assert z.stat().st_size < 300_000 + 110_000          # limite + au plus un fichier
        with zipfile.ZipFile(z) as zf:
            assert all(n.startswith("CentreA/2026-10-01/") for n in zf.namelist())  # arborescence de images_traitees/
    contenu = _lire(sortie / "export" / "contenu_export.csv")
    assert len({c["fichier"] for c in contenu}) == 6
    assert not list((sortie / "export").glob("*.partiel"))


def test_reprise_n_archive_que_le_nouveau(tmp_path):
    sortie = _jeu(tmp_path, n=3)
    ex.exporter(sortie, log=lambda *_: None)
    avant = _lire(sortie / "export" / "manifest_export.csv")
    assert len(avant) == 1
    # relance sans nouveauté : rien de créé
    ex.exporter(sortie, log=lambda *_: None)
    assert _lire(sortie / "export" / "manifest_export.csv") == avant
    # archive déjà transférée puis supprimée : n'est pas refaite
    (sortie / "export" / avant[0]["archive"]).unlink()
    ex.exporter(sortie, log=lambda *_: None)
    assert not list((sortie / "export").glob("*.zip"))
    # nouvelle image : une seule nouvelle archive, numéro suivant
    nouveau = sortie / "images_traitees" / "CentreA" / "2026-10-01" / "zzzz0000" / "nouveau.dcm"
    nouveau.parent.mkdir(parents=True)
    nouveau.write_bytes(os.urandom(5000))
    ex.exporter(sortie, log=lambda *_: None)
    man = _lire(sortie / "export" / "manifest_export.csv")
    assert [m["archive"] for m in man][-1].endswith("_part002.zip") and len(man) == 2
    assert man[-1]["nb_fichiers"] == "1"
    # --refaire : tout est réarchivé
    ex.exporter(sortie, refaire=True, log=lambda *_: None)
    assert sum(int(m["nb_fichiers"]) for m in _lire(sortie / "export" / "manifest_export.csv")) == 4


def test_archive_partielle_jamais_conservee(tmp_path):
    sortie = _jeu(tmp_path, n=2)
    (sortie / "export").mkdir()
    (sortie / "export" / "fredd_CentreA_2026-10-01_part001.zip.partiel").write_bytes(b"coupure en cours d'ecriture")
    ex.exporter(sortie, log=lambda *_: None)
    assert not list((sortie / "export").glob("*.partiel"))
    assert len(_lire(sortie / "export" / "manifest_export.csv")) == 1


def test_filtres_centre_et_date(tmp_path):
    sortie = _jeu(tmp_path, n=2, centre="CentreA", date="2026-10-01")
    for centre, date in (("CentreA", "2026-10-16"), ("CentreB", "2026-10-16")):
        p = sortie / "images_traitees" / centre / date / "x1y2z3w4" / "i.dcm"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(os.urandom(2000))
    ex.exporter(sortie, date="2026-10-16", log=lambda *_: None)
    assert {(m["centre"], m["date"]) for m in _lire(sortie / "export" / "manifest_export.csv")} == {
        ("CentreA", "2026-10-16"), ("CentreB", "2026-10-16")}
    ex.exporter(sortie, centre="CentreA", log=lambda *_: None)      # complète avec le dossier du 01/10 seulement
    man = _lire(sortie / "export" / "manifest_export.csv")
    assert ("CentreA", "2026-10-01") in {(m["centre"], m["date"]) for m in man} and len(man) == 3


def test_seul_images_traitees_est_exporte(tmp_path):
    sortie = _jeu(tmp_path, n=1)
    for sous in ("rapports", "sans_patient/CentreA", "quarantaine_pseudonymisation/CentreA/a1", "echecs_traitement/CentreA/a1"):
        (sortie / sous).mkdir(parents=True)
        (sortie / sous / "secret.dcm").write_bytes(b"x" * 100)
    ex.exporter(sortie, log=lambda *_: None)
    noms = [n for z in (sortie / "export").glob("*.zip") for n in zipfile.ZipFile(z).namelist()]
    assert noms and not any("secret" in n for n in noms)


def test_controle_detecte_archive_modifiee_ou_manquante(tmp_path):
    sortie = _jeu(tmp_path, n=4)
    ex.exporter(sortie, taille_max_go=0.0002, log=lambda *_: None)
    dossier = sortie / "export"
    assert ex.controler(dossier, log=lambda *_: None) == 0
    zips = sorted(dossier.glob("*.zip"))
    assert len(zips) >= 2
    data = bytearray(zips[0].read_bytes())
    data[len(data) // 2] ^= 0xFF                                    # un octet altéré, même taille
    zips[0].write_bytes(bytes(data))
    zips[1].unlink()
    messages = []
    assert ex.controler(dossier, log=messages.append) == 1
    texte = "\n".join(messages)
    assert "SOMME" in texte and "MANQUANTE" in texte


def test_rien_a_exporter(tmp_path):
    assert ex.exporter(tmp_path / "vide", log=lambda *_: None) == 2
    (tmp_path / "s" / "images_traitees").mkdir(parents=True)
    assert ex.exporter(tmp_path / "s", log=lambda *_: None) == 2
    assert ex.exporter(tmp_path / "s", taille_max_go=0, log=lambda *_: None) == 2


def test_commande_exporter_via_fredd(tmp_path, monkeypatch):
    """python fredd.py exporter lit la sortie dans config_fredd.ini et accepte les options."""
    import fredd
    sortie = _jeu(tmp_path, n=2)
    (tmp_path / "images").mkdir()
    (tmp_path / "d.csv").write_text("id\nx\n", encoding="utf-8")
    cfg = tmp_path / "config_fredd.ini"
    cfg.write_text(f"[dossiers]\nimages = {tmp_path / 'images'}\ndonnees = {tmp_path / 'd.csv'}\nsortie = {sortie}\n"
                   "[colonnes]\nidentifiant = id\ncentre = centre\nskezia = fredd\n[options]\ntaille_paquet = 200\nautres =\n",
                   encoding="utf-8")
    monkeypatch.setattr(fredd, "CONFIG", cfg)
    assert fredd.main(["exporter", "--taille-go", "1"]) == 0
    assert (sortie / "export" / "manifest_export.csv").is_file()
    assert "exporter" in fredd.COMMANDES

