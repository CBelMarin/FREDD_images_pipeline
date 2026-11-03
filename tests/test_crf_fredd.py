"""Confrontation avec un export au format du CRF FREDD (page_depot_images,
dictionnaire Survey_Lille.csv) : modalité, œil et orientation de coupe."""
import io

import pydicom
import pytest

from conftest import FAKE_RARETIA, dicom_bytes, dossier_patient, e2e_bytes, ecrire, lire_csv

import pipeline_fredd as pf

# En-tête réaliste : identifiant, site, 1 colonne écrite avec des points (variante
# d'export), colonnes AF « champ maculaire » volontairement absentes.
ENTETE = ("id;leg_site_inc;leg_site_inc_nom;adm_nom_naissance;"
          "dep_FO.dep_dispo_FO.dep_dispo_FO_OD;dep_FO_dep_dispo_FO_dep_dispo_FO_OG;"
          "dep_dispo_OCT_dep_dispo_OCT_mac_dep_dispo_OCT_OD;dep_dispo_OCT_dep_dispo_OCT_mac_dep_dispo_OCT_OG;"
          "dep_dispo_OCT_dep_dispo_OCT_temp_dep_dispo_OCT_OD;dep_dispo_OCT_dep_dispo_OCT_temp_dep_dispo_OCT_OG;"
          "dep_dispo_AF_dep_dispo_AF_gc_dep_dispo_AF_OD;dep_dispo_AF_dep_dispo_AF_gc_dep_dispo_AF_OG\n")

UUID1 = "a1b2c3d4-c067-400a-b148-0141a13f641d"


def _dicom_lat(lat, seed):
    ds = pydicom.dcmread(io.BytesIO(dicom_bytes(seed=seed)))
    ds.ImageLaterality = lat
    buf = io.BytesIO()
    ds.save_as(buf)
    return buf.getvalue()


@pytest.fixture
def run_crf(tmp_path):
    racine = tmp_path / "images"
    # Patient 1 — dossier = UUID complet, fichiers = ID tronqué + suffixes (convention FREDD)
    p1 = racine / "Lille" / UUID1
    ecrire(p1 / "a1b2c3d4_OD_fundus.dcm", _dicom_lat("R", 1))
    ecrire(p1 / "a1b2c3d4_OCT_OD.e2e", e2e_bytes(series=[
        dict(serie=1, motif="OCT B-SCAN", lat="R", bscans=[(0, 5, 20, 5)], n_oct=1),      # ligne horizontale
        dict(serie=2, motif="OCT B-SCAN", lat="R", bscans=[(10, 0, 10, 20)], n_oct=1),    # ligne verticale
    ]))
    # Patient 2 — fichier directement dans le centre, ID en préfixe, rien déclaré
    ecrire(racine / "Lille" / "b2c3d4e5_OG_fo.dcm", _dicom_lat("L", 2))
    # Discordance dossier / fichier -> mise de côté
    ecrire(racine / "Lille" / UUID1 / "b2c3d4e5_OD.dcm", dicom_bytes(seed=3))

    data = tmp_path / "export.csv"
    data.write_text(ENTETE +
                    f"{UUID1};12;CHU Lille;NOM_INTERDIT;1;1;1;0;0;0;1;0\n"
                    "b2c3d4e5-0000-0000-0000-000000000000;12;CHU Lille;NOM_INTERDIT;0;0;0;0;0;0;0;0\n",
                    encoding="utf-8")
    sortie = tmp_path / "s"

    def _run(*extra):
        return pf.main([str(racine), "--data", str(data), "--sortie", str(sortie), "--raretia-path",
                        str(FAKE_RARETIA), "--cols-skezia", "fredd", "--col-centre", "leg_site_inc_nom", "--centre-depuis", "dossier", *extra])
    return _run, sortie


def test_preset_fredd_oeil_et_orientation(run_crf):
    run, sortie = run_crf
    assert run() == 0
    conf = {r["id_fredd"][:8]: r for r in lire_csv(sortie / "rapports" / "confrontation_patients.csv")}

    d1 = conf["a1b2c3d4"]["detail_par_modalite"]
    assert "fundus OD: déclaré=oui / trouvées=1 / traitées=1 / ID discordant=1 -> OK" in d1
    assert "fundus OG: déclaré=oui / trouvées=0 / traitées=0 -> DECLAREE_NON_TROUVEE" in d1
    assert "oct_bscan OD horizontale: déclaré=oui / trouvées=1 -> OK" in d1
    assert "oct_bscan OD verticale: déclaré=non / trouvées=1 -> TROUVEE_NON_DECLAREE" in d1
    assert "oct_bscan OG horizontale: déclaré=non / trouvées=0 -> OK" in d1
    assert "autofluorescence OD grand_champ: déclaré=oui / trouvées=0 -> DECLAREE_NON_TROUVEE" in d1
    assert conf["a1b2c3d4"]["nb_ecarts_modalite"] == "3"
    assert conf["a1b2c3d4"]["centre_coherent"] == "Oui"   # 'CHU Lille' <-> dossier 'Lille'
    assert conf["a1b2c3d4"]["id_fredd"] == UUID1          # ID complet du CSV restitué

    d2 = conf["b2c3d4e5"]["detail_par_modalite"]
    assert "fundus OG: déclaré=non / trouvées=1 / traitées=1 -> TROUVEE_NON_DECLAREE" in d2
    assert conf["b2c3d4e5"]["statut_confrontation"] == "FREDD_SEULEMENT"

    md = (sortie / "rapports" / "rapport_execution.md").read_text(encoding="utf-8")
    assert "dep_dispo_AF_dep_dispo_AF_mac_dep_dispo_AF_OD" in md  # colonne absente signalée
    for f in (sortie / "rapports").rglob("*.csv"):
        assert "NOM_INTERDIT" not in f.read_text(encoding="utf-8-sig")


def test_convention_id_et_discordance(run_crf):
    run, sortie = run_crf
    assert run() == 0
    j = {r["chemin_source"].replace("\\", "/").split("/")[-1]: r
         for r in lire_csv(sortie / "rapports" / "journal_images.csv")}
    assert j["a1b2c3d4_OD_fundus.dcm"]["statut"] == "TRAITEE_OK"
    assert j["a1b2c3d4_OD_fundus.dcm"]["id_fredd"] == UUID1
    assert j["b2c3d4e5_OG_fo.dcm"]["statut"] == "TRAITEE_OK"
    disc = j["b2c3d4e5_OD.dcm"]
    assert disc["statut"] == "MISE_DE_COTE_ID_AMBIGU"
    assert "discordants" in disc["observation"]
    # le DICOM traité est rangé sous l'ID FREDD complet
    assert dossier_patient(sortie, "Lille", UUID1).is_dir()
    # l'image discordante n'est comptée « trouvée » pour AUCUN des deux patients,
    # mais elle est signalée chez chacun
    conf = {r["id_fredd"][:8]: r for r in lire_csv(sortie / "rapports" / "confrontation_patients.csv")}
    assert conf["a1b2c3d4"]["nb_images_id_discordant"] == "1"
    assert conf["b2c3d4e5"]["nb_images_id_discordant"] == "1"
    assert conf["b2c3d4e5"]["nb_images_inventoriees"] == "1"  # seulement b2c3d4e5_OG_fo.dcm


def test_preset_fredd_sans_aucune_colonne(tmp_path):
    p = tmp_path / "d.csv"
    p.write_text("id;autre\na1b2c3d4;x\n", encoding="utf-8")
    with pytest.raises(ValueError, match="Aucune colonne du CRF FREDD"):
        pf.DonneesFredd.charger(p, cols_skezia="fredd")


@pytest.mark.parametrize("spec,msg", [("c:fundus:XX", "Œil inconnu"), ("c:oct_bscan:OD:diagonale", "Orientation")])
def test_spec_invalide(tmp_path, spec, msg):
    p = tmp_path / "d.csv"
    p.write_text("id;c\na1b2c3d4;1\n", encoding="utf-8")
    with pytest.raises(ValueError, match=msg):
        pf.DonneesFredd.charger(p, cols_skezia=spec)
