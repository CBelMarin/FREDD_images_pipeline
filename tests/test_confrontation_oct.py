"""confrontation OCT = horizontale / verticale / autre ; écarts typés et corrections par centre."""
import csv
from types import SimpleNamespace

import pipeline_fredd as pf

ID = "a1b2c3d4-1111-4222-8333-944455556666"
COLS = [("oct_mac_OD", "oct_bscan", "OD", "horizontale"), ("oct_temp_OD", "oct_bscan", "OD", "verticale"),
        ("oct_autre_OD", "oct_bscan", "OD", "autre")]


def _confronter(declare, contenus, centre="CARGO-HUS"):
    skezia = dict(zip(("oct_mac_OD", "oct_temp_OD", "oct_autre_OD"), declare))
    donnees = SimpleNamespace(patients={"a1b2c3d4": {"id": ID, "centre": centre, "skezia": skezia}},
                              cols_skezia=COLS, centre_depuis="donnees")
    recs = [{"patient_id_detecte": "a1b2c3d4", "statut": "A_TRAITER", "centre": centre,
             "_contenu": [dict(modalite="oct_bscan", lateralite="OD", orientation=o, erreur="") for o in contenus]}]
    return pf.etape5_confrontation(recs, [], donnees)[0]


def test_une_radiale_ne_valide_plus_horizontale_ni_verticale():
    l = _confronter(["oui", "oui", "non"], ["radiale/mixte"])
    assert "oct_bscan OD horizontale: déclaré=oui / trouvées=0 / autres orientations trouvées : radiale/mixte=1 " \
           "-> DECLAREE_ORIENTATION_DIFFERENTE" in l["detail_par_modalite"]
    assert "oct_bscan OD verticale: déclaré=oui / trouvées=0 / autres orientations trouvées : radiale/mixte=1 " \
           "-> DECLAREE_ORIENTATION_DIFFERENTE" in l["detail_par_modalite"]
    assert "oct_bscan OD autre: déclaré=non / trouvées=1 -> TROUVEE_NON_DECLAREE" in l["detail_par_modalite"]


def test_autre_declare_et_trouve_est_ok_et_la_pile_horizontale_compte_horizontale():
    l = _confronter(["oui", "non", "oui"], ["volume, lignes horizontales", "oblique"])
    assert l["nb_ecarts_modalite"] == 0
    assert "oct_bscan OD horizontale: déclaré=oui / trouvées=1 (dont piles=1) -> OK" in l["detail_par_modalite"]


def test_fichier_de_corrections_par_centre(tmp_path):
    l = _confronter(["oui", "oui", "non"], ["radiale/mixte"], centre="Lille")
    pf.ecrire_corrections_centres(tmp_path, [l])
    f = tmp_path / "corrections_crf" / "Lille.csv"
    lignes = list(csv.DictReader(open(f, encoding="utf-8-sig"), delimiter=";"))
    assert {x["cible"] for x in lignes} == {"oct_bscan OD horizontale", "oct_bscan OD verticale", "oct_bscan OD autre"}
    assert all(x["id_fredd"] == ID for x in lignes)
    assert {x["constat"] for x in lignes} >= {"Déclaré dans le CRF, mais les OCT trouvés ont une autre orientation"}


def test_pile_declaree_sous_autre_seulement_n_est_pas_un_ecart():
    """CRF : « Autre (volume, radiale, oblique etc) » : un volume déclaré sous « autre » seulement est conforme."""
    l = _confronter(["non", "non", "oui"], ["volume, lignes horizontales"])
    assert l["nb_ecarts_modalite"] == 0
    assert "piles seulement" in l["detail_par_modalite"]


def test_pile_declaree_nulle_part_est_signalee_sous_autre():
    l = _confronter(["non", "non", "non"], ["volume, lignes horizontales"])
    assert "oct_bscan OD autre: déclaré=non / trouvées=1 (dont piles=1) -> TROUVEE_NON_DECLAREE" in l["detail_par_modalite"]
    assert "oct_bscan OD horizontale: déclaré=non / trouvées=1 (dont piles=1) / piles seulement" in l["detail_par_modalite"]


def test_coupe_horizontale_simple_non_declaree_reste_un_ecart():
    l = _confronter(["non", "non", "non"], ["horizontale"])
    assert "oct_bscan OD horizontale: déclaré=non / trouvées=1 -> TROUVEE_NON_DECLAREE" in l["detail_par_modalite"]
