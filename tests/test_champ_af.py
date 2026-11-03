"""AF maculaire (30°) / grand champ (55°) distinguées par les 4 coins (règle du module)."""
import numpy as np

from pipeline_wp2.reader import champ_par_coins
from pipeline_wp2.detection import compute_angle_of_view
import pipeline_fredd as pf
from test_confrontation_oct import ID
from types import SimpleNamespace


def _cercle(taille=400):
    y, x = np.ogrid[:taille, :taille]
    img = np.zeros((taille, taille), np.uint8)
    img[(x - taille / 2) ** 2 + (y - taille / 2) ** 2 < (taille * 0.42) ** 2] = 120
    return img


def test_coins_noirs_grand_champ_et_plein_cadre_maculaire():
    assert champ_par_coins(_cercle()) == "grand_champ"
    assert champ_par_coins(np.full((400, 400), 110, np.uint8)) == "maculaire"


def test_angle_af_d_apres_le_champ_et_modele_prioritaire():
    assert compute_angle_of_view({"champ": "grand_champ"}, "autofluorescence") == 55.0
    assert compute_angle_of_view({"champ": "maculaire"}, "autofluorescence") == 30.0
    assert compute_angle_of_view({}, "autofluorescence") is None                 # rien d'inventé
    assert compute_angle_of_view({"model": "Optos California"}, "fundus_widefield") == 200.0
    assert compute_angle_of_view({"camera_angle": 45.0, "champ": "grand_champ"}, "autofluorescence") == 45.0  # mesure d'abord


def _conf(declare_mac, declare_gc, champs, centre="CARGO-HUS"):
    cols = [("af_mac_OD", "autofluorescence", "OD", "maculaire"), ("af_gc_OD", "autofluorescence", "OD", "grand_champ")]
    donnees = SimpleNamespace(patients={"a1b2c3d4": {"id": ID, "centre": centre,
                                                     "skezia": {"af_mac_OD": declare_mac, "af_gc_OD": declare_gc}}},
                              cols_skezia=cols, centre_depuis="donnees")
    recs = [{"patient_id_detecte": "a1b2c3d4", "statut": "TRAITEE_OK", "centre": centre, "_contenu": []}]
    sorties = [{"id_fredd": ID, "image_type": "autofluorescence", "lateralite": "OD", "champ": c,
                "rangement": "images_traitees"} for c in champs]
    return pf.etape5_confrontation(recs, sorties, donnees)[0]


def test_confrontation_af_distingue_les_deux_champs():
    l = _conf("oui", "oui", ["grand_champ"])
    assert "autofluorescence OD maculaire: déclaré=oui / trouvées=0 / autre champ=1 -> DECLAREE_CHAMP_DIFFERENT" \
        in l["detail_par_modalite"]
    assert "autofluorescence OD grand_champ: déclaré=oui / trouvées=1 -> OK" in l["detail_par_modalite"]
    assert l["nb_ecarts_modalite"] == 1
    assert _conf("oui", "oui", ["maculaire", "grand_champ"])["nb_ecarts_modalite"] == 0
    assert "TROUVEE_NON_DECLAREE" in _conf("non", "non", ["maculaire"])["detail_par_modalite"]
