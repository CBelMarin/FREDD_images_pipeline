"""--strates-detaillees couvre les orientations d'OCT et les familles d'appareils."""
import echantillon as ech


def _l(pid, mod, orientation="", appareil="", centre="A"):
    return {"centre": centre, "id_fredd": pid, "statut_tri": "A_TRAITER", "categorie": "E2E_HEIDELBERG",
            "modalite": mod, "orientation": orientation, "appareil": appareil, "erreur": ""}


def test_les_strates_detaillees_forcent_la_couverture_des_orientations_rares():
    lignes = [_l(f"h{i:07d}", "oct_bscan", "horizontale") for i in range(40)]
    lignes += [_l("v0000000", "oct_bscan", "verticale"), _l("p0000000", "oct_bscan", "volume, lignes horizontales"),
               _l("r0000000", "oct_bscan", "radiale/mixte"), _l("o0000000", "fundus", appareil="Optos California")]
    sans, _ = ech.tirer(lignes, 5, 1, 1)
    avec, eff = ech.tirer(lignes, 5, 1, 1, detail=True)
    ids = {p["id_fredd"] for p in avec}
    assert {"v0000000", "p0000000", "r0000000", "o0000000"} <= ids          # chaque cas rare est couvert
    types = {e["type"] for e in eff}
    assert {"E2E_HEIDELBERG:oct_bscan:verticale", "E2E_HEIDELBERG:oct_bscan:pile",
            "E2E_HEIDELBERG:oct_bscan:radiale", "E2E_HEIDELBERG:fundus:optos"} <= types
    assert all(t.count(":") == 1 for p in sans for t in p["types"].split(" | "))   # défaut inchangé
