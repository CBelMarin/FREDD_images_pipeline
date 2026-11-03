"""Confrontation : appareil déclaré (Clarus / Optos / Heidelberg) et « AF dans le même E2E que l'OCT »."""
import csv
from types import SimpleNamespace

import pipeline_fredd as pf

ID = "a1b2c3d4-1111-4222-8333-944455556666"
COLS = [("FO_OD", "fundus", "OD", None), ("AF_OD", "autofluorescence", "OD", None)]
APP = [("app_FO_OD", "fundus", "OD"), ("app_AF_OD", "autofluorescence", "OD")]


def _c(modalite, appareil, oeil="OD"):
    return dict(modalite=modalite, lateralite=oeil, orientation="", appareil=appareil, erreur="")


def _conf(extra, recs_contenu, skezia=None, centre="Lille"):
    donnees = SimpleNamespace(
        patients={"a1b2c3d4": {"id": ID, "centre": centre, "skezia": skezia or {"FO_OD": "1", "AF_OD": "1"}, "extra": extra}},
        cols_skezia=COLS, cols_appareil=APP, col_af_oct="dep_AF_OCT", centre_depuis="donnees")
    recs = [{"patient_id_detecte": "a1b2c3d4", "statut": "A_TRAITER", "centre": centre,
             "categorie": cat, "_contenu": cont} for cat, cont in recs_contenu]
    return pf.etape5_confrontation(recs, [], donnees)[0]


def test_familles_d_appareil():
    assert pf.appareil_declare("2", "fundus") == "optos" and pf.appareil_declare("3", "fundus") == "autre"
    assert pf.appareil_declare("3", "autofluorescence") == "heidelberg" and pf.appareil_declare("Clarus", "fundus") == "clarus"
    assert pf.appareil_declare("", "fundus") == ""
    assert pf.appareil_trouve("Carl Zeiss Meditec CLARUS 500") == "clarus"
    assert pf.appareil_trouve("Optos plc daytona") == "optos" and pf.appareil_trouve("Heidelberg Engineering Spectralis") == "heidelberg"
    assert pf.appareil_trouve("") == "" and pf.appareil_trouve("Topcon") == "autre"


def test_appareil_conforme_et_different():
    ok = _conf({"app_FO_OD": "1"}, [("DICOM", [_c("fundus", "Carl Zeiss Meditec CLARUS 500")])])
    assert "appareil fundus OD: déclaré=clarus / trouvé=clarus -> OK" in ok["detail_par_modalite"]
    ko = _conf({"app_FO_OD": "2"}, [("DICOM", [_c("fundus", "Carl Zeiss Meditec CLARUS 500")])])
    assert "appareil fundus OD: déclaré=optos / trouvé=clarus -> APPAREIL_DIFFERENT" in ko["detail_par_modalite"]
    assert ko["_ecarts"][-1]["verdict"] == "APPAREIL_DIFFERENT"


def test_autre_declare_alors_que_clarus_trouve_est_un_ecart_et_appareil_inconnu_non_evaluable():
    ko = _conf({"app_FO_OD": "3"}, [("DICOM", [_c("fundus", "Carl Zeiss Meditec CLARUS 500")])])
    assert "déclaré=autre / trouvé=clarus -> APPAREIL_DIFFERENT" in ko["detail_par_modalite"]
    inconnu = _conf({"app_FO_OD": "1"}, [("DICOM", [_c("fundus", "")])], skezia={"FO_OD": "1", "AF_OD": "0"})
    assert "-> NON_EVALUABLE" in inconnu["detail_par_modalite"] and inconnu["nb_ecarts_modalite"] == 0


def test_appareil_ignore_sans_image_ou_sans_declaration():
    sans_image = _conf({"app_FO_OD": "1"}, [("DICOM", [_c("oct_bscan", "Heidelberg Spectralis")])], skezia={"FO_OD": "0", "AF_OD": "0"})
    assert "appareil fundus" not in sans_image["detail_par_modalite"]
    sans_decl = _conf({}, [("DICOM", [_c("fundus", "Carl Zeiss Meditec CLARUS 500")])])
    assert "appareil fundus" not in sans_decl["detail_par_modalite"]


def test_af_et_oct_dans_le_meme_e2e():
    meme = [("E2E_HEIDELBERG", [_c("autofluorescence", "Heidelberg"), _c("oct_bscan", "Heidelberg")])]
    separes = [("E2E_HEIDELBERG", [_c("autofluorescence", "Heidelberg")]), ("E2E_HEIDELBERG", [_c("oct_bscan", "Heidelberg")])]
    assert "-> OK" in _conf({"dep_AF_OCT": "1"}, meme)["detail_par_modalite"].split("AF dans le même E2E que l'OCT")[1]
    l = _conf({"dep_AF_OCT": "1"}, separes)
    assert "déclaré=oui / trouvé=non -> DECLAREE_MEME_FICHIER_NON_TROUVE" in l["detail_par_modalite"]
    l = _conf({"dep_AF_OCT": "0"}, meme)
    assert "déclaré=non / trouvé=oui -> MEME_FICHIER_NON_DECLARE" in l["detail_par_modalite"]
    l = _conf({"dep_AF_OCT": "1"}, [("E2E_HEIDELBERG", [_c("oct_bscan", "Heidelberg")])])   # pas d'AF : non évaluable
    assert "-> NON_EVALUABLE" in l["detail_par_modalite"] and "DECLAREE_MEME" not in l["detail_par_modalite"]
    assert "AF dans le même E2E" not in _conf({}, meme)["detail_par_modalite"]


def test_corrections_par_centre_pour_les_nouveaux_ecarts(tmp_path):
    l = _conf({"app_FO_OD": "2"}, [("DICOM", [_c("fundus", "Carl Zeiss Meditec CLARUS 500")])])
    pf.ecrire_corrections_centres(tmp_path, [l])
    lignes = list(csv.DictReader(open(tmp_path / "corrections_crf" / "Lille.csv", encoding="utf-8-sig"), delimiter=";"))
    assert any(x["constat"].startswith("Appareil déclaré") and x["images_trouvees"] == "clarus" for x in lignes)


def test_chargement_des_colonnes_du_survey(tmp_path):
    f = tmp_path / "d.csv"
    f.write_text("id;leg_site_inc;dep_FO_dep_dispo_FO_dep_dispo_FO_OD;dep_app_FO_OD;dep_AF_OCT\n"
                 f"{ID};2;1;2;1\n", encoding="utf-8")
    d = pf.DonneesFredd.charger(str(f), cols_skezia="fredd")
    assert d.cols_appareil == [("dep_app_FO_OD", "fundus", "OD")] and d.col_af_oct == "dep_AF_OCT"
    p = next(iter(d.patients.values()))
    assert p["extra"] == {"dep_app_FO_OD": "2", "dep_AF_OCT": "1"}
    assert "dep_app_AF_OD" in d.cols_skezia_absentes     # colonne facultative absente : signalée, pas bloquante
