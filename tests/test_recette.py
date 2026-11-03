"""L'outil de recette doit DÉTECTER un écart, et seulement les vrais écarts."""
import csv
from collections import Counter

import pytest

import recette
from conftest import ecrire, lire_csv
from test_verite_terrain import MANIFESTE, U1, U2, construire


@pytest.fixture
def jeu(tmp_path):
    racine, data = construire(tmp_path)
    modele = tmp_path / "verite.csv"
    assert recette.main(["modele", str(racine), "--sortie", str(modele)]) == 0
    return tmp_path, racine, data, modele


def _remplir(modele, valeurs):
    lignes = lire_csv(modele)
    for l in lignes:
        if l["chemin_relatif"] in valeurs:
            l["id_attendu"], l["contenu_attendu"] = valeurs[l["chemin_relatif"]]
    with open(modele, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=recette.COLONNES_MODELE, delimiter=";")
        w.writeheader()
        w.writerows(lignes)


def test_modele_liste_toutes_les_images_colonnes_vides(jeu):
    _t, racine, _d, modele = jeu
    lignes = lire_csv(modele)
    assert {l["chemin_relatif"] for l in lignes} == {f"images/{c}" for c in MANIFESTE}
    assert all(l["id_attendu"] == "" and l["contenu_attendu"] == "" for l in lignes)   # jamais pré-rempli


def _verifier(tmp, racine, data, modele):
    return recette.main(["verifier", str(racine), "--data", str(data), "--verite", str(modele),
                         "--sortie", str(tmp / "recette"), "--col-centre", "leg_site_inc_nom"])


def test_recette_conforme(jeu):
    tmp, racine, data, modele = jeu
    _remplir(modele, {
        f"images/Lille/{U1}/{U1}_OD.e2e": (U1, "oct_bscan OD horizontale; autofluorescence OD"),
        f"images/Lille/{U1}/300b72a1_OG_FO.dcm": ("300b72a1", "fundus OG"),
        "images/Lille/4c5d6e7f/4c5d6e7f_OG.e2e": (U2, "oct_bscan OG volume"),
        "images/Lille/4c5d6e7f/9a8b7c6d_OD.dcm": ("-", "fundus OD"),
        "images/Lille/divers/DUPONT_JEAN.dcm": ("-", ""),
    })
    assert _verifier(tmp, racine, data, modele) == 0
    res = lire_csv(tmp / "recette" / "recette_resultats.csv")
    # 5 identifiants + 4 contenus réellement vérifiés (et pas « conforme » faute de vérification)
    assert Counter(r["critere"] for r in res if r["resultat"] == "OK") == \
        Counter(inventaire=len(MANIFESTE), identifiant=5, contenu=4)


def test_verite_vide_jamais_conforme(jeu):
    tmp, racine, data, modele = jeu          # modèle laissé vide
    assert _verifier(tmp, racine, data, modele) == 2


@pytest.mark.parametrize("cle,valeur,critere", [
    (f"images/Lille/{U1}/{U1}_OD.e2e", (U2, ""), "identifiant"),                                  # mauvais ID
    (f"images/Lille/{U1}/{U1}_OD.e2e", ("", "oct_bscan OD verticale; autofluorescence OD"), "contenu"),  # orientation
    (f"images/Lille/{U1}/{U1}_OD.e2e", ("", "oct_bscan OG horizontale; autofluorescence OD"), "contenu"),  # œil
    (f"images/Lille/{U1}/{U1}_OD.e2e", ("", "oct_bscan OD horizontale"), "contenu"),              # AF en trop
    (f"images/Lille/{U1}/300b72a1_OG_FO.dcm", ("", "fundus OG; autofluorescence OG"), "contenu"),  # série manquante
    ("images/Lille/4c5d6e7f/9a8b7c6d_OD.dcm", ("9a8b7c6d", ""), "identifiant"),                   # attendait un rattachement
    ("images/Lille/divers/DUPONT_JEAN.dcm", (U1, ""), "identifiant"),
    (f"images/Lille/{U1}/300b72a1_OG_FO.dcm", ("", "fundus OG en_biais"), "contenu"),             # saisie invalide
])
def test_recette_detecte_l_ecart(jeu, cle, valeur, critere):
    tmp, racine, data, modele = jeu
    _remplir(modele, {cle: valeur})
    assert _verifier(tmp, racine, data, modele) == 1
    res = lire_csv(tmp / "recette" / "recette_resultats.csv")
    ecarts = [r for r in res if r["resultat"] != "OK"]
    assert [(r["chemin_relatif"], r["critere"]) for r in ecarts] == [(cle, critere)]


def test_fichier_absent_de_l_inventaire(jeu):
    tmp, racine, data, modele = jeu
    with open(modele, "a", encoding="utf-8-sig") as f:
        f.write("images/Lille/inexistant.dcm;-;;\n")
    assert _verifier(tmp, racine, data, modele) == 1


@pytest.mark.parametrize("txt,attendu", [
    ("oct_bscan OD horizontale", ("oct_bscan", "OD", "horizontale")),
    ("oct_bscan od volume, lignes verticales", ("oct_bscan", "OD", "volume, lignes verticales")),
    ("autofluorescence", ("autofluorescence", None, None)),
    ("fundus OG", ("fundus", "OG", None)),
    ("  ", None),
])
def test_parse_serie(txt, attendu):
    assert recette.parse_serie(txt) == attendu
