"""Tests unitaires : convention d'ID FREDD et analyse du contenu des fichiers."""
import pytest

import analyse_contenu as ac
import pipeline_fredd as pf
from conftest import dicom_bytes, e2e_bytes


# ───────────── Convention d'ID (préfixe) ─────────────

@pytest.mark.parametrize("nom,attendu", [
    ("300b72a1-c067-400a-b148-0141a13f641d", "300b72a1"),
    ("300b72a1-c067-400a-b148-0141a13f641d_OCT", "300b72a1"),
    ("300b72a1_OD_fundus", "300b72a1"),          # cas que l'ancienne heuristique RATAIT ('_')
    ("300B72A1 OG", "300b72a1"),
    ("300b72a1", "300b72a1"),
    ("20231005_OCT", ""),                          # date AAAAMMJJ
    ("300b72a12", ""),                             # 9 caractères hexa : pas un ID tronqué
    ("OCT_300b72a1", ""),                          # pas en préfixe
    ("divers", ""),
])
def test_id_en_prefixe(nom, attendu):
    assert pf.id_en_prefixe(nom) == attendu


def test_pseudo_sous_centre():
    f = pf.pseudo_sous_centre
    assert f("/r/C/300b72a1-c067-400a-b148-0141a13f641d/300b72a1_OD.dcm", "/r") == ("300b72a1", "", False)
    assert f("/r/C/300b72a1_OD.dcm", "/r") == ("300b72a1", "", False)
    assert f("/r/C.zip :: 300b72a1/x.zip :: 300b72a1_OG.e2e", "/r")[0] == "300b72a1"
    pid, obs, disc = f("/r/C/300b72a1/4c5d6e7f_OD.dcm", "/r")
    assert disc and "300b72a1" in obs and "4c5d6e7f" in obs
    # dossier daté + fichier conforme : pas de discordance
    assert f("/r/C/20231005/300b72a1_OD.dcm", "/r") == ("300b72a1", "", False)
    # repli hors convention, signalé
    pid, obs, disc = f("/r/C/divers/OCT_300b72a1.dcm", "/r")
    assert pid == "300b72a1" and "hors convention" in obs and not disc
    # le centre n'est jamais un pseudonyme
    assert f("/r/abcdef12/x.dcm", "/r")[0] == ""
    # dossier conforme mais fichier non renommé : rattaché, avec signalement
    pid, obs, disc = f("/r/C/300b72a1/IMG_0001.dcm", "/r")
    assert pid == "300b72a1" and "non renommé" in obs and not disc


# ───────────── Analyse du contenu ─────────────

def _e2e(tmp_path, series, **kw):
    p = tmp_path / "x.e2e"
    p.write_bytes(e2e_bytes(series=series, **kw))
    return p


@pytest.mark.parametrize("en_face,attendu", [
    ("Autofluorescence", "autofluorescence"), ("FAF", "autofluorescence"), ("BluePeak", "autofluorescence"),
    ("Infra-Red", "infrarouge"), ("IR", "infrarouge"), ("Fluorescein Angiography", "angiographie_fluo"),
    ("ICGA", "icga"), ("MultiColor", "fundus_multicolor"),
])
def test_e2e_modalite_en_face(tmp_path, en_face, attendu):
    res = ac.analyser_e2e(_e2e(tmp_path, [dict(serie=1, en_face=en_face, lat="L", n_fond=1)]))
    assert [(r["modalite"], r["lateralite"]) for r in res] == [(attendu, "OG")]


@pytest.mark.parametrize("bscans,attendu", [
    ([(0, 5, 20, 5)], "horizontale"),
    ([(10, 0, 10, 20)], "verticale"),
    ([(0, 0, 20, 20)], "oblique"),
    ([(0, 5, 20, 5), (10, 0, 10, 20), (0, 0, 20, 20)], "radiale/mixte"),
    ([(0, i, 20, i) for i in range(5)], "volume, lignes horizontales"),
])
def test_e2e_orientation(tmp_path, bscans, attendu):
    res = ac.analyser_e2e(_e2e(tmp_path, [dict(serie=1, motif="OCT B-SCAN", lat="R", bscans=bscans, n_oct=len(bscans))]))
    assert res[0]["modalite"] == "oct_bscan" and res[0]["orientation"] == attendu


def test_e2e_orientation_bscan_degenere_reconstruit_via_centre(tmp_path):
    """Bug d'export réel trouvé sur des fichiers Heidelberg réels (deux fichiers réels,
    B-scan isolé hors volume, numImages=0) : posX2/posY2 sont
    identiques à posX1/posY1 (segment dégénéré), alors que centrePosX/Y reste
    correct. Sans reconstruction, l'orientation serait '' (segment nul)."""
    seg_degenere_horizontal = (-3.0, -1.661, -3.0, -1.661, 3.0, -1.661)  # centre décalé en X -> horizontale
    res = ac.analyser_e2e(_e2e(tmp_path, [dict(serie=1, motif="OCT B-SCAN", lat="R",
                                                bscans=[seg_degenere_horizontal], n_oct=1)]))
    assert res[0]["orientation"] == "horizontale"

    seg_degenere_vertical = (10.0, -5.0, 10.0, -5.0, 10.0, 5.0)  # centre décalé en Y -> verticale
    res = ac.analyser_e2e(_e2e(tmp_path, [dict(serie=1, motif="OCT B-SCAN", lat="R",
                                                bscans=[seg_degenere_vertical], n_oct=1)]))
    assert res[0]["orientation"] == "verticale"


def test_e2e_lateralite_contradictoire_et_identite_jamais_lue(tmp_path):
    p = _e2e(tmp_path, [dict(serie=1, motif="OCT B-SCAN", lat="R", bscans=[(0, 0, 1, 0)], n_oct=1),
                        dict(serie=1, lat="L")], nom_patient="SECRETNOM")
    res = ac.analyser_e2e(p)
    assert res[0]["lateralite"] == "contradictoire"
    assert "SECRETNOM" not in repr(res)


def test_e2e_illisible(tmp_path):
    p = tmp_path / "x.e2e"
    p.write_bytes(b"pas un e2e" * 10)
    res = ac.analyser(p, "E2E_HEIDELBERG", "/x/a1b2c3d4_OD.e2e")
    assert res[0]["erreur"] and res[0]["lateralite"] == "OD"  # œil repris du nom de fichier


def test_dicom_af_et_lateralite(tmp_path):
    import io
    import pydicom
    ds = pydicom.dcmread(io.BytesIO(dicom_bytes()))
    ds.ImageType = ["ORIGINAL", "PRIMARY", "FAF"]
    ds.ImageLaterality = "L"
    p = tmp_path / "x.dcm"
    ds.save_as(str(p))
    r = ac.analyser(p, "DICOM")[0]
    assert (r["modalite"], r["lateralite"]) == ("autofluorescence", "OG")


@pytest.mark.parametrize("nom,attendu", [("a1b2c3d4_OD_fo.jpg", "OD"), ("x OS.png", "OG"), ("od_og.jpg", ""),
                                         ("photo.jpg", ""), ("a.zip :: b/a1_OG.dcm", "OG")])
def test_oeil_depuis_nom(nom, attendu):
    assert ac.oeil_depuis_nom(nom) == attendu


def test_e2e_synthetique_relu_par_eyepy(tmp_path):
    """Contrôle croisé : le fichier E2E synthétique doit être lu de la même
    façon par une implémentation indépendante (eyepy). Sans cela, les tests
    ci-dessus ne vérifieraient l'analyseur que contre lui-même."""
    he = pytest.importorskip("eyepy.io.he")
    p = _e2e(tmp_path, [dict(serie=1, motif="OCT B-SCAN", lat="R", bscans=[(0, 5, 20, 5), (0, 6, 20, 6)],
                             n_oct=2, n_fond=1),
                        dict(serie=2, en_face="Autofluorescence", lat="L", n_fond=1)])
    nous = {r["serie"].split("_")[1]: r for r in ac.analyser_e2e(p)}
    with he.HeE2eReader(p) as lecteur:
        eux = {str(s.id): s for s in lecteur.series}
    assert set(nous) == set(eux) == {"1", "2"}
    lat = {82: "OD", 76: "OG"}
    for k in nous:
        assert nous[k]["lateralite"] == lat[int(eux[k].laterality())]
    assert eux["1"].n_bscans == nous["1"]["nb_bscans"] == 2
    assert eux["2"].enface_modality() == "Autofluorescence" and nous["2"]["modalite"] == "autofluorescence"


def test_diagnostic_e2e_sans_identite(tmp_path, monkeypatch, capsys):
    import sys
    import diagnostic_e2e
    p = _e2e(tmp_path, [dict(serie=1, motif="OCT B-SCAN", lat="R", bscans=[(0, 5, 20, 5)], n_oct=1)],
             nom_patient="SECRETNOM")
    sortie = tmp_path / "diag.csv"
    monkeypatch.setattr(sys, "argv", ["diagnostic_e2e.py", str(p), "--sortie", str(sortie)])
    diagnostic_e2e.main()
    texte = sortie.read_text(encoding="utf-8-sig") + capsys.readouterr().out
    assert "oct_bscan OD horizontale" in texte and "t11:R" in texte
    assert "SECRETNOM" not in texte and "IPP987654321" not in texte


def test_inventaire_autonome_analyse_contenu(tmp_path, monkeypatch):
    """inventaire_images.py --analyse-contenu : contenu des E2E/DICOM, y compris dans un zip."""
    import sys
    import zipfile
    import inventaire_images
    from conftest import ecrire, lire_csv
    racine = tmp_path / "images"
    ecrire(racine / "Lille" / "300b72a1" / "300b72a1_oct.e2e",
           e2e_bytes(series=[dict(serie=1, motif="OCT", lat="R", bscans=[(10, 0, 10, 20)], n_oct=1),
                             dict(serie=2, en_face="FAF", lat="R", n_fond=1)], nom_patient="SECRETNOM"))
    z = racine / "Nantes.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("4c5d6e7f/4c5d6e7f_OG.dcm", dicom_bytes())
    sortie = tmp_path / "s"
    monkeypatch.setattr(sys, "argv", ["inventaire_images.py", str(racine), "--sortie", str(sortie),
                                      "--analyse-contenu"])
    inventaire_images.main()
    lignes = lire_csv(next(sortie.glob("contenu_images_*.csv")))
    vu = sorted((l["modalite"], l["lateralite"], l["orientation"]) for l in lignes)
    assert vu == [("autofluorescence", "OD", ""), ("fundus", "OG", ""), ("oct_bscan", "OD", "verticale")]
    assert "SECRETNOM" not in next(sortie.glob("contenu_images_*.csv")).read_text(encoding="utf-8-sig")


def test_inventaire_autonome_sans_option_reste_sans_dependance(tmp_path, monkeypatch):
    import sys
    import inventaire_images
    from conftest import ecrire
    ecrire(tmp_path / "images" / "C" / "a.e2e", b"x")
    monkeypatch.setattr(sys, "argv", ["inventaire_images.py", str(tmp_path / "images"), "--sortie", str(tmp_path / "s")])
    sys.modules.pop("analyse_contenu", None)
    inventaire_images.main()
    assert "analyse_contenu" not in sys.modules
    assert not list((tmp_path / "s").glob("contenu_images_*.csv"))
