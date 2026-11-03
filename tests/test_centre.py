"""Centre de chaque image pris dans les données FREDD (défaut quand une colonne
centre est connue) : les dépôts réels arrivent rangés par LOT, pas par centre
(cas du dossier « 1 »)."""
from conftest import FAKE_RARETIA, dicom_bytes, ecrire, lire_csv, zip_bytes
import pipeline_fredd as pf

P1 = "a1b2c3d4-0000-0000-0000-000000000001"   # CHU Lille
P2 = "b2c3d4e5-0000-0000-0000-000000000002"   # CHU Nantes
P3 = "c3d4e5f6-0000-0000-0000-000000000003"   # centre non renseigné
P4 = "d4e5f6a7"                                # absent des données


def _lancer(tmp_path, *extra):
    racine = tmp_path / "1"
    # structure du dossier réel : lot2/lot2/<pseudo>/<UID>_1.dcm, et des lots zippés
    ecrire(racine / "lot2" / "lot2" / P1[:8] / "1.2.276.0.75.2.2.70.0.3.111_1.dcm", dicom_bytes(seed=1))
    ecrire(racine / "lot2" / "lot2" / P3[:8] / "1.2.276.0.75.2.2.70.0.3.333_1.dcm", dicom_bytes(seed=3))
    ecrire(racine / "lot2" / "lot2" / P4 / "1.2.276.0.75.2.2.70.0.3.444_1.dcm", dicom_bytes(seed=4))
    ecrire(racine / "lot16.zip", zip_bytes({f"{P2[:8]}/1.2.276.0.75.2.2.70.0.3.222_1.dcm": dicom_bytes(seed=2)}))
    data = tmp_path / "d.csv"
    data.write_text(f"id;centre;img_retino\n{P1};CHU Lille;oui\n{P2};CHU Nantes;oui\n{P3};;oui\n",
                    encoding="utf-8")
    sortie = tmp_path / "sortie"
    assert pf.main([str(racine), "--data", str(data), "--sortie", str(sortie), "--raretia-path", str(FAKE_RARETIA),
                    "--cols-skezia", "img_retino:fundus", "--col-centre", "centre", *extra]) == 0
    j = {(l["id_fredd"] or l["patient_id_detecte"])[:8]: l for l in lire_csv(sortie / "rapports" / "journal_images.csv")}
    return sortie, j


def test_centre_pris_dans_les_donnees_par_defaut(tmp_path):
    sortie, j = _lancer(tmp_path)
    assert j[P1[:8]]["centre"] == "CHU Lille" and j[P1[:8]]["lot"].endswith("lot2")
    assert j[P2[:8]]["centre"] == "CHU Nantes"
    dest = {k: l["destination"].replace("\\", "/") for k, l in j.items()}
    assert dest[P1[:8]].startswith("images_traitees/" + pf.safe_name("CHU Lille") + "/")
    assert dest[P2[:8]].startswith("images_traitees/" + pf.safe_name("CHU Nantes") + "/")
    assert {l["centre"] for l in lire_csv(sortie / "rapports" / "bilan_par_centre.csv")} >= {"CHU Lille", "CHU Nantes"}


def test_centre_absent_des_donnees_repli_sur_le_dossier(tmp_path):
    _, j = _lancer(tmp_path)
    assert j[P3[:8]]["centre"] == "lot2"
    assert "centre absent des données" in j[P3[:8]]["observation"]


def test_image_sans_patient_reste_sous_son_dossier(tmp_path):
    _, j = _lancer(tmp_path)
    assert j[P4]["statut"] == "MISE_DE_COTE_SANS_PATIENT"
    assert j[P4]["destination"].replace("\\", "/").startswith("sans_patient/lot2/")


def test_coherence_centre_sans_objet(tmp_path):
    sortie, _ = _lancer(tmp_path)
    conf = {l["id_fredd"][:8]: l for l in lire_csv(sortie / "rapports" / "confrontation_patients.csv")}
    assert conf[P1[:8]]["centre_coherent"].startswith("N/A")


def test_mode_dossier_conserve(tmp_path):
    _, j = _lancer(tmp_path, "--centre-depuis", "dossier")
    assert j[P1[:8]]["centre"] == "lot2" and j[P2[:8]]["centre"] == "lot16"


# ───────────── Fusion de libellés d'un même centre (HUS = CARGO-HUS) ─────────────

def _lancer_alias(tmp_path, *extra):
    racine = tmp_path / "1"
    ecrire(racine / "lot2" / "lot2" / P1[:8] / "a_1.dcm", dicom_bytes(seed=1))
    ecrire(racine / "lot2" / "lot2" / P2[:8] / "b_1.dcm", dicom_bytes(seed=2))
    data = tmp_path / "d.csv"
    data.write_text(f"id;centre;img_retino\n{P1};HUS;oui\n{P2};CARGO-HUS;oui\n", encoding="utf-8")
    sortie = tmp_path / "sortie"
    assert pf.main([str(racine), "--data", str(data), "--sortie", str(sortie), "--raretia-path", str(FAKE_RARETIA),
                    "--cols-skezia", "img_retino:fundus", "--col-centre", "centre", *extra]) == 0
    return {l["id_fredd"][:8]: l["centre"] for l in lire_csv(sortie / "rapports" / "journal_images.csv")}, sortie


def test_alias_fichier_livre_hus_cargo(tmp_path):
    centres, sortie = _lancer_alias(tmp_path)   # alias_centres.csv livré avec le pipeline
    assert centres == {P1[:8]: "CARGO-HUS", P2[:8]: "CARGO-HUS"}
    assert [p.name for p in (sortie / "images_traitees").iterdir()] == ["CARGO-HUS"]


def test_alias_desactivable(tmp_path):
    centres, _ = _lancer_alias(tmp_path, "--alias-centres", "aucun")
    assert centres == {P1[:8]: "HUS", P2[:8]: "CARGO-HUS"}


def test_alias_texte_et_normalisation(tmp_path):
    alias, _ = pf.charger_alias_centres("chu de lille=CHU Lille ; Hôpital Nord = CHU Lille")
    assert pf.canon_centre("CHU  DE LILLE", alias) == "CHU Lille"
    assert pf.canon_centre("hopital-nord", alias) == "CHU Lille"
    assert pf.canon_centre("Nantes", alias) == "Nantes"
    f = tmp_path / "a.csv"
    f.write_text("# commentaire\nnom_source;nom_retenu\nHUS;CARGO-HUS\n", encoding="utf-8")
    alias, origine = pf.charger_alias_centres(str(f))
    assert alias == {"hus": "CARGO-HUS"} and origine == "a.csv"


ALIAS_NUMEROS = "1=REFERET-HNV;2=CARGO-HUS;3=MAOLYA-Montpellier;5=MAOLYA-Lille"


def test_numeros_de_centre_convertis_en_noms():
    alias, _ = pf.charger_alias_centres(ALIAS_NUMEROS)
    attendu = {"1": "REFERET-HNV", "2": "CARGO-HUS", "3": "MAOLYA-Montpellier", "5": "MAOLYA-Lille"}
    for numero, nom in attendu.items():
        assert pf.canon_centre(numero, alias) == nom
        assert pf.canon_centre(f" {numero} ", alias) == nom
        assert pf.canon_centre(f"{numero}.0", alias) == nom          # colonne numérique relue comme décimale
        assert pf.canon_centre(f"{numero},00", alias) == nom
    assert pf.canon_centre("4", alias) == "4"                        # numéro sans règle : inchangé
    assert pf.canon_centre("12", alias) == "12" and pf.canon_centre("10", alias) == "10"   # pas de collision avec 1


def test_numero_de_centre_dans_les_donnees_donne_le_nom_retenu(tmp_path):
    racine = tmp_path / "images"
    ecrire(racine / "lot" / P1[:8] / "1.2.3_1.dcm", dicom_bytes(seed=1))
    ecrire(racine / "lot" / P2[:8] / "1.2.3_2.dcm", dicom_bytes(seed=2))
    data = tmp_path / "d.csv"
    data.write_text(f"id;centre;img_retino\n{P1};2;oui\n{P2};5.0;oui\n", encoding="utf-8")
    sortie = tmp_path / "sortie"
    assert pf.main([str(racine), "--data", str(data), "--sortie", str(sortie), "--raretia-path", str(FAKE_RARETIA),
                    "--cols-skezia", "img_retino:fundus", "--col-centre", "centre",
                    "--alias-centres", ALIAS_NUMEROS]) == 0
    centres = {l["id_fredd"][:8]: l["centre"] for l in lire_csv(sortie / "rapports" / "journal_images.csv")}
    assert centres == {P1[:8]: "CARGO-HUS", P2[:8]: "MAOLYA-Lille"}
    assert sorted(p.name for p in (sortie / "images_traitees").iterdir()) == ["CARGO-HUS", "MAOLYA-Lille"]
