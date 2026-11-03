"""Tests unitaires : lecture du CSV FREDD, adaptateur raretia, utilitaires."""
import pytest

import pipeline_fredd as pf
import raretia_adapter as ra


# ───────────── Données FREDD ─────────────

def test_csv_excel_cp1252_virgule(tmp_path):
    p = tmp_path / "d.csv"
    p.write_bytes("Patient_ID,Centre,Imagerie_OCT\nA1B2C3D4,Hôpital Nord,Oui\n".encode("cp1252"))
    d = pf.DonneesFredd.charger(p, cols_skezia="Imagerie_OCT")
    assert d.encodage == "cp1252"
    assert d.col_id == "Patient_ID" and d.col_centre == "Centre"
    assert d.patients["a1b2c3d4"]["centre"] == "Hôpital Nord"


def test_uuid_canonique_et_ambiguite(tmp_path):
    p = tmp_path / "d.csv"
    p.write_text("id\n"
                 "a1b2c3d4-0000-0000-0000-000000000001\n"
                 "a1b2c3d4-0000-0000-0000-000000000002\n"   # même préfixe 8 car. -> ambigu
                 "b2c3d4e5-0000-0000-0000-000000000003\n"
                 "b2c3d4e5-0000-0000-0000-000000000003\n"   # ligne répétée : pas ambigu
                 "\n;\n", encoding="utf-8")
    d = pf.DonneesFredd.charger(p)
    assert "a1b2c3d4" in d.ambigus and "b2c3d4e5" not in d.ambigus
    assert d.patients["b2c3d4e5"]["id"] == "b2c3d4e5-0000-0000-0000-000000000003"


def test_colonne_absente_message_clair(tmp_path):
    p = tmp_path / "d.csv"
    p.write_text("foo;bar\n1;2\n", encoding="utf-8")
    with pytest.raises(ValueError, match="Colonnes disponibles : foo, bar"):
        pf.DonneesFredd.charger(p)


@pytest.mark.parametrize("v,attendu", [
    ("oui", True), ("Oui", True), ("1", True), ("x", True), ("2", True), ("2021-05-03", True),
    ("non", False), ("0", False), ("Non", False), ("", None), ("NA", None),
])
def test_valeur_declaree(v, attendu):
    assert pf.valeur_declaree(v) is attendu


# ───────────── Utilitaires ─────────────

def test_centre_depuis_lot():
    assert pf.centre_depuis_lot("images/CentreA") == "CentreA"
    assert pf.centre_depuis_lot("images/CentreB (zip)") == "CentreB"
    assert pf.centre_depuis_lot("images/(racine)") == "_centre_inconnu"


def test_parties_relatives_zip():
    parts = pf.parties_relatives("/r/C/a.zip :: x/../b.zip :: y/z:1.dcm", "/r")
    assert parts == ["C", "a.zip", "x", "b.zip", "y", "z_1.dcm"]


# ───────────── Adaptateur raretia ─────────────

@pytest.mark.parametrize("v,attendu", [
    ("OUI", True), ("À VÉRIFIER", False), ("A_VERIFIER", False), ("True", True), ("False", False), ("?", None),
])
def test_parse_accepted(v, attendu):
    assert ra.parse_accepted(v) is attendu


def test_rapport_raretia_alias_francais(tmp_path):
    p = tmp_path / "r.csv"
    p.write_text("Fichier source;Type image;Score qualité;Statut;Motif;Fichier sortie\n"
                 "x.dcm;fundus_color;0.5;À VÉRIFIER;sombre;x_0.dcm\n", encoding="utf-8")
    rows = ra.read_raretia_report(p)
    assert rows[0]["source"] == "x.dcm" and rows[0]["output"] == "x_0.dcm"
    assert ra.parse_accepted(rows[0]["accepted"]) is False


def test_csv_quelconque_ignore(tmp_path):
    p = tmp_path / "pairs.csv"
    p.write_text("a;b\n1;2\n", encoding="utf-8")
    assert ra.read_raretia_report(p) is None


def test_rattachement_par_prefixe_sans_rapport(tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "abc__123456789012_0_fundus.dcm").write_bytes(b"x")
    (tmp_path / "autre.dcm").write_bytes(b"x")
    res = ra.ResultatPaquet(returncode=0, log_path=tmp_path / "l", duree_s=0)
    ra._rattacher_sorties(tmp_path, {"abc__123456789012"}, res)
    assert len(res.sorties["abc__123456789012"]) == 1


def test_rattachement_structure_reelle_pipeline_wp2(tmp_path):
    """Reproduit la structure RÉELLE de sortie de pipeline_wp2 (vérifiée le
    par exécution sur un DICOM synthétique) : <sortie>/<stem>/
    avec reports/ et processed/<patient_id>/ comme deux dossiers FRÈRES,
    et output_file relatif à <sortie>/<stem>/ (pas au dossier du rapport).
    Non-régression du bug où output_file était cherché relativement au
    dossier du rapport (csv_path.parent) au lieu de csv_path.parent.parent."""
    stem_dir = tmp_path / "staging"  # <sortie>/<stem(entree)>/
    reports = stem_dir / "reports"
    processed = stem_dir / "processed" / "abc__123456789012"
    reports.mkdir(parents=True)
    processed.mkdir(parents=True)
    out_dcm = processed / "abc__123456789012_R_fundus_color_000.dcm"
    out_dcm.write_bytes(b"x")
    (reports / "rapport_complet.csv").write_text(
        "source_file;image_index;image_type;laterality;quality_score;quality_issues;"
        "accepted;rejection_reason;output_file\n"
        "abc__123456789012.dcm;0;fundus_color;R;0.8;;OUI;;"
        "processed/abc__123456789012/abc__123456789012_R_fundus_color_000.dcm\n",
        encoding="utf-8",
    )
    res = ra.ResultatPaquet(returncode=0, log_path=tmp_path / "l", duree_s=0)
    ra._rattacher_sorties(tmp_path, {"abc__123456789012"}, res)
    sorties = res.sorties["abc__123456789012"]
    assert len(sorties) == 1
    assert sorties[0].chemin == out_dcm


def test_dossier_sans_image_arret_explicite(tmp_path, capsys):
    (tmp_path / "vide").mkdir()
    data = tmp_path / "d.csv"
    data.write_text("id\na1b2c3d4\n", encoding="utf-8")
    rc = pf.main([str(tmp_path / "vide"), "--data", str(data), "--sortie", str(tmp_path / "s"), "--simulation"])
    assert rc == 3
    assert "Aucune image trouvée" in capsys.readouterr().out


def test_progression_en_direct(tmp_path):
    """Les lignes « i / n - fichier » du module pipeline_wp2 sont remontées au fil
    de l'eau (suivi à l'écran), sans attendre la fin du paquet."""
    from conftest import FAKE_RARETIA, dicom_bytes
    entree = tmp_path / "s"
    entree.mkdir()
    for k in range(3):
        (entree / f"abc__{k:012d}.dcm").write_bytes(dicom_bytes(seed=k))
    vus = []
    res = ra.run_raretia(entree, tmp_path / "b", {f"abc__{k:012d}" for k in range(3)}, tmp_path / "l.log",
                         raretia_path=FAKE_RARETIA, progression=lambda i, n: vus.append((i, n)))
    assert res.returncode == 0
    assert vus == [(0, 3), (1, 3), (2, 3)]
    assert len(res.sorties) == 3


def test_suivi_affichage_hors_console(capsys):
    s = pf.SuiviProgression("paquet 1/1")
    for i in range(20):
        s(i, 20)
    s.terminer()
    lignes = [l for l in capsys.readouterr().out.splitlines() if l.strip()]
    assert 5 <= len(lignes) <= 11                  # une ligne par palier de 10 %, pas 20
    assert "fichier 1/20" in lignes[0] and "reste" in lignes[-1]


def test_timeout_paquet(tmp_path):
    import sys
    entree = tmp_path / "s"
    entree.mkdir()
    lent = tmp_path / "lent"
    (lent / "lent").mkdir(parents=True)
    (lent / "lent" / "__init__.py").write_text("")
    (lent / "lent" / "main.py").write_text("import time\nprint('0 / 1  -  x', flush=True)\ntime.sleep(30)\n")
    res = ra.run_raretia(entree, tmp_path / "b", set(), tmp_path / "l.log", raretia_path=lent,
                         module="lent.main", timeout_s=2)
    assert res.returncode == -9 and "Timeout" in res.message


def test_orientation_coupe_dicom(tmp_path):
    """Lecture de l'orientation de coupe écrite par raretia dans ImageComments."""
    import pipeline_fredd
    from conftest import dicom_bytes, ecrire
    for comm, attendu in [("B-scan 3/49 ; orientation : horizontale", "horizontale"),
                          ("B-scan 1/3 ; orientation : volume, lignes horizontales", "volume, lignes horizontales"),
                          ("B-scan 2/3", ""), ("", "")]:
        p = tmp_path / "x.dcm"
        ecrire(p, dicom_bytes(comments=comm, modality="OPT", rgb=False))
        assert pipeline_fredd.orientation_coupe_dicom(p) == attendu
    assert pipeline_fredd.orientation_coupe_dicom(tmp_path / "absent.dcm") == ""
    assert "orientation_coupe" in pipeline_fredd.SORTIES_HEADERS
