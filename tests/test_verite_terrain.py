"""VÉRITÉ TERRAIN de bout en bout, étape par étape.

Un manifeste (MANIFESTE ci-dessous) décrit, pour chaque fichier d'un jeu
d'essai, ce qui est ATTENDU à chaque étape :
  1   inventorié ou non ;
  1b  contenu (modalité, œil, orientation, alerte) ;
  2   statut de tri et identifiant FREDD retenu ;
  3   statut final, nombre de DICOM produits, rangement par centre/patient,
      PatientID des sorties ;
  4   cohérence des totaux du rapport ;
  5   verdict de confrontation SKEZIA par patient, modalité, œil, orientation.
Le pipeline est lancé une fois en simulation et une fois en réel (faux
raretia), puis comparé au manifeste LIGNE À LIGNE. Ajouter un cas = ajouter
une entrée au manifeste.
"""
import io
import json
import re
import zipfile
from collections import Counter
from pathlib import Path

import pydicom
import pytest

from conftest import FAKE_RARETIA, dicom_bytes, e2e_bytes, ecrire, lire_csv

U1 = "300b72a1-c067-400a-b148-0141a13f641d"
U1_AUTRE = "300b72a1-ffff-4fff-8fff-ffffffffffff"
U2 = "4c5d6e7f-1111-4222-8333-944455556666"
U3 = "9a8b7c6d-aaaa-4bbb-8ccc-dddd00001111"
U4 = "c0ffee00-2222-4333-8444-555566667777"
U5 = "5e5e5e5e-3333-4444-8555-666677778888"

H, V = [(0, 5, 20, 5)], [(10, 0, 10, 20)]


def dcm(lat=None, seed=0, **tags):
    ds = pydicom.dcmread(io.BytesIO(dicom_bytes(seed=seed)))
    if lat:
        ds.ImageLaterality = lat
    for k, v in tags.items():
        setattr(ds, k, v)
    b = io.BytesIO()
    ds.save_as(b)
    return b.getvalue()


E2E_U1 = e2e_bytes([dict(serie=1, motif="OCT Line", lat="R", bscans=H, n_oct=1, n_fond=1),
                    dict(serie=2, en_face="Autofluorescence", lat="R", n_fond=1)])
E2E_U3_TRONQUE = e2e_bytes([dict(serie=1, motif="OCT Line", lat="L", bscans=H, n_oct=1)])[:-5]


def zip_de(entrees):
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        for n, d in entrees.items():
            z.writestr(n, d)
    return b.getvalue()


# chemin (sous la racine ; ' :: ' = entrée de zip) -> attendu
#   contenu : ensemble de (modalite, oeil, orientation, mot-clé d'alerte ou "")
#   tri     : statut après l'étape 2 en simulation
#   final   : statut après traitement réel ; sorties : nb de DICOM produits
MANIFESTE = {
    f"Lille/{U1}/{U1}_OD.e2e": dict(
        octets=E2E_U1, id=U1, tri="A_TRAITER_SIMULATION", final="TRAITEE_OK", sorties=4, doublon="g1",  # 3 B-scans OCT + 1 AF
        contenu={("oct_bscan", "OD", "horizontale", ""), ("autofluorescence", "OD", "", "")}),
    f"Lille/{U1}/copie/{U1}_OD.e2e": dict(            # copie binaire du précédent
        octets=E2E_U1, id=U1, tri="A_TRAITER_SIMULATION", final="TRAITEE_OK", sorties=4, doublon="g1",  # 3 B-scans OCT + 1 AF
        contenu={("oct_bscan", "OD", "horizontale", ""), ("autofluorescence", "OD", "", "")}),
    f"Lille/{U1}/300b72a1_OG_FO.dcm": dict(
        octets=dcm("L", 1), id=U1, tri="A_TRAITER_SIMULATION", final="TRAITEE_OK", sorties=1,
        contenu={("fundus", "OG", "", "")}),
    f"Lille/{U1}/300b72a1_OD_retino.dcm": dict(       # nom dit OD, DICOM dit L
        octets=dcm("L", 2), id=U1, tri="A_TRAITER_SIMULATION", final="TRAITEE_OK", sorties=1,
        contenu={("fundus", "OG", "", "≠")}),
    f"Lille/{U1}/IMG_0001.jpg": dict(
        octets=b"\xff\xd8\xff\xe0" + b"\0" * 64, id=U1, tri="NON_TRAITEE_FORMAT", final="NON_TRAITEE_FORMAT",
        sorties=0, contenu={("image_standard", "", "", "")}, obs="non renommé"),
    f"Lille/4c5d6e7f/4c5d6e7f_OG.e2e": dict(
        octets=e2e_bytes([dict(serie=1, motif="Volume", lat="L", bscans=[(i, 0, i, 20) for i in range(5)], n_oct=5)]),
        id=U2, tri="A_TRAITER_SIMULATION", final="TRAITEE_OK", sorties=3,
        contenu={("oct_bscan", "OG", "volume, lignes verticales", "")}),
    f"Lille/4c5d6e7f/9a8b7c6d_OD.dcm": dict(           # rangé chez un autre patient
        octets=dcm("R", 3), id="", tri="MISE_DE_COTE_ID_AMBIGU", final="MISE_DE_COTE_ID_AMBIGU", sorties=0,
        contenu={("fundus", "OD", "", "")}, rangement="sans_patient/Lille/_id_ambigu"),
    f"Lille/{U1_AUTRE}/x.dcm": dict(                   # même préfixe que U1, autre UUID
        octets=dcm("R", 4), id="", tri="MISE_DE_COTE_ID_AMBIGU", final="MISE_DE_COTE_ID_AMBIGU", sorties=0,
        contenu={("fundus", "OD", "", "")}, rangement="sans_patient/Lille/_id_ambigu"),
    "Lille/12345678/12345678_OD.dcm": dict(            # absent des données
        octets=dcm("R", 5), id="", tri="MISE_DE_COTE_SANS_PATIENT", final="MISE_DE_COTE_SANS_PATIENT", sorties=0,
        contenu={("fundus", "OD", "", "")}, rangement="sans_patient/Lille"),
    "Lille/divers/DUPONT_JEAN.dcm": dict(               # aucun ID
        octets=dcm(None, 6), id="", tri="MISE_DE_COTE_ID_NON_DETECTE", final="MISE_DE_COTE_ID_NON_DETECTE",
        sorties=0, contenu={("fundus", "", "", "")}, rangement="sans_patient/Lille/_id_non_detecte"),
    # Angiographie (Modality OP) : l'inventaire la reconnaît. Hors périmètre FREDD :
    # non envoyée à raretia ; elle ne satisfait donc pas un « fond d'œil OD » déclaré.
    "Lille/5e5e5e5e/5e5e5e5e_OD_angio.dcm": dict(
        octets=dcm("R", 8, SeriesDescription="Fluorescein Angiography"), id=U5, tri="NON_TRAITEE_ANGIO",
        final="NON_TRAITEE_ANGIO", sorties=0, contenu={("angiographie_fluo", "OD", "", "")}),
    "Nantes.zip :: 9a8b7c6d/examen.zip :: 9a8b7c6d_OD_FAF.dcm": dict(
        octets=dcm("R", 7, ImageType=["ORIGINAL", "PRIMARY", "FAF"], ImageComments="CHAMP_GC"), id=U3, tri="A_TRAITER_SIMULATION",
        final="TRAITEE_OK", sorties=1, contenu={("autofluorescence", "OD", "", "")}),
    "Nantes.zip :: 9a8b7c6d/9a8b7c6d_OG.e2e": dict(
        octets=E2E_U3_TRONQUE, id=U3, tri="A_TRAITER_SIMULATION", final="TRAITEE_OK", sorties=3,
        contenu={("oct_bscan", "OG", "horizontale", "tronqué")}),
}
NON_IMAGES = {"Lille/notes.txt": b"pas une image", "Lille/abime.zip": b"PK\x03\x04 corrompu"}

COLS = ["id", "leg_site_inc_nom",
        "dep_FO_dep_dispo_FO_dep_dispo_FO_OD", "dep_FO_dep_dispo_FO_dep_dispo_FO_OG",
        "dep_dispo_OCT_dep_dispo_OCT_mac_dep_dispo_OCT_OD", "dep_dispo_OCT_dep_dispo_OCT_mac_dep_dispo_OCT_OG",
        "dep_dispo_OCT_dep_dispo_OCT_temp_dep_dispo_OCT_OD", "dep_dispo_OCT_dep_dispo_OCT_temp_dep_dispo_OCT_OG",
        "dep_dispo_AF_dep_dispo_AF_mac_dep_dispo_AF_OD", "dep_dispo_AF_dep_dispo_AF_mac_dep_dispo_AF_OG",
        "dep_dispo_AF_dep_dispo_AF_gc_dep_dispo_AF_OD", "dep_dispo_AF_dep_dispo_AF_gc_dep_dispo_AF_OG"]
#              FO OD/OG   OCT-H OD/OG  OCT-V OD/OG  AFmac OD/OG  AFgc OD/OG
DONNEES = [[U1, "CHU Lille", 0, 1, 1, 0, 0, 0, 1, 0, 0, 0],
           [U2, "CHU Lille", 1, 0, 0, 1, 0, 1, 0, 0, 0, 0],
           [U3, "CHU Nantes", 1, 0, 0, 1, 0, 0, 0, 0, 1, 0],
           [U4, "CHU Nantes", 1, 0, 0, 0, 0, 0, 0, 0, 0, 0],
           [U5, "CHU Lille", 1, 0, 0, 0, 0, 0, 0, 0, 0, 0]]

# Étape 5 : verdicts attendus (les combinaisons non listées doivent être OK)
CONFRONTATION = {
    U1: dict(statut="SKEZIA_ET_FREDD", ecarts={}),
    U2: dict(statut="SKEZIA_ET_FREDD", ecarts={"fundus OD": "A_VERIFIER_ID_DISCORDANT",
                                               "oct_bscan OG horizontale": "DECLAREE_ORIENTATION_DIFFERENTE"}),  # OG n'a qu'un volume vertical
    U3: dict(statut="SKEZIA_ET_FREDD", ecarts={"fundus OD": "A_VERIFIER_ID_DISCORDANT"}),
    U4: dict(statut="SKEZIA_SEULEMENT", ecarts={"fundus OD": "DECLAREE_NON_TROUVEE"}),
    U5: dict(statut="SKEZIA_ET_FREDD", ecarts={"fundus OD": "DECLAREE_NON_TROUVEE"}),
    "12345678": dict(statut="IMAGES_HORS_DONNEES", ecarts=None),
}


def construire(base):
    racine = base / "images"
    zips = {}
    for rel, cas in MANIFESTE.items():
        morceaux = rel.split(" :: ")
        if len(morceaux) == 1:
            ecrire(racine / rel, cas["octets"])
        else:
            zips.setdefault(morceaux[0], []).append((morceaux[1:], cas["octets"]))
    for rel, octets in NON_IMAGES.items():
        ecrire(racine / rel, octets)
    for zrel, entrees in zips.items():
        externe = {}
        internes = {}
        for chemin, octets in entrees:
            if len(chemin) == 1:
                externe[chemin[0]] = octets
            else:
                internes.setdefault(chemin[0], {})[chemin[1]] = octets
        for nom, contenu in internes.items():
            externe[nom] = zip_de(contenu)
        ecrire(racine / zrel, zip_de(externe))
    data = base / "export.csv"
    data.write_text(";".join(COLS) + "\n" + "\n".join(";".join(map(str, l)) for l in DONNEES) + "\n", encoding="utf-8")
    return racine, data


def _lancer(base, simulation):
    import pipeline_fredd
    racine, data = construire(base)
    sortie = base / "sortie"
    argv = [str(racine), "--data", str(data), "--sortie", str(sortie), "--col-centre", "leg_site_inc_nom", "--centre-depuis", "dossier",
            "--cols-skezia", "fredd"] + (["--simulation"] if simulation else ["--raretia-path", str(FAKE_RARETIA)])
    assert pipeline_fredd.main(argv) == 0
    rap = sortie / "rapports"

    def cle(chemin):
        # `racine` est un WindowsPath -> `str(racine)` utilise des antislashs sous
        # Windows ; `chemin` (chemin_source, chemin OS natif) aussi. L'ancien code
        # cherchait un antislash suivi d'un "/" littéral, qui ne matchait jamais
        # sous Windows (bug découvert en recette sur un poste Windows).
        chemin = chemin.replace("\\", "/")
        prefixe = str(racine).replace("\\", "/") + "/"
        return chemin[len(prefixe):] if chemin.startswith(prefixe) else chemin
    journal = {cle(r["chemin_source"]): r for r in lire_csv(rap / "journal_images.csv")}
    contenu = {}
    for c in lire_csv(sortie / "01_inventaire" / "contenu_images.csv"):
        contenu.setdefault(cle(c["chemin_source"]), []).append(c)
    return dict(sortie=sortie, racine=racine, journal=journal, contenu=contenu,
                sorties=lire_csv(rap / "sorties_traitees.csv"),
                confrontation={r["id_fredd"]: r for r in lire_csv(rap / "confrontation_patients.csv")},
                execution=json.loads((rap / "execution.json").read_text(encoding="utf-8")))


@pytest.fixture(scope="module")
def simu(tmp_path_factory):
    return _lancer(tmp_path_factory.mktemp("vt_simu"), simulation=True)


@pytest.fixture(scope="module")
def reel(tmp_path_factory):
    return _lancer(tmp_path_factory.mktemp("vt_reel"), simulation=False)


CAS = sorted(MANIFESTE)


# ───────── Étape 1 : inventaire ─────────

def test_etape1_inventaire_exhaustif_et_exact(simu):
    assert set(simu["journal"]) == set(MANIFESTE)          # rien de manquant, rien en trop


def test_etape1_erreurs_tracees(simu):
    err = lire_csv(next((simu["sortie"] / "01_inventaire").glob("erreurs_inventaire_*.csv")))
    assert any(e["chemin"].endswith("abime.zip") for e in err)


# ───────── Étape 1b : métadonnées ─────────

@pytest.mark.parametrize("cas", CAS)
def test_etape1b_contenu(simu, cas):
    j = simu["journal"][cas]
    if j["statut"] == "IGNOREE_DOUBLON":
        pytest.skip("copie binaire : contenu analysé une seule fois (sur l'original)")
    obtenu = {(c["modalite"], c["lateralite"], c["orientation"]) for c in simu["contenu"][cas]}
    attendu = {(m, o, r) for m, o, r, _a in MANIFESTE[cas]["contenu"]}
    assert obtenu == attendu
    for m, o, r, alerte in MANIFESTE[cas]["contenu"]:
        a = next(c["alerte"] for c in simu["contenu"][cas] if (c["modalite"], c["lateralite"], c["orientation"]) == (m, o, r))
        assert (alerte in a) if alerte else a == "", a


# ───────── Étape 2 : identifiant + tri ─────────

@pytest.mark.parametrize("cas", CAS)
def test_etape2_identifiant_et_tri(simu, cas):
    j, att = simu["journal"][cas], MANIFESTE[cas]
    if att.get("doublon"):
        assert j["statut"] in (att["tri"], "IGNOREE_DOUBLON")
    else:
        assert j["statut"] == att["tri"], j["observation"]
    assert j["id_fredd"] == att["id"], j["observation"]
    if att.get("obs"):
        assert att["obs"] in j["observation"]


def test_etape2_un_seul_doublon_retenu(simu):
    groupes = {}
    for cas, att in MANIFESTE.items():
        if att.get("doublon"):
            groupes.setdefault(att["doublon"], []).append(simu["journal"][cas]["statut"])
    for statuts in groupes.values():
        assert statuts.count("IGNOREE_DOUBLON") == len(statuts) - 1


@pytest.mark.parametrize("cas", [c for c in CAS if MANIFESTE[c].get("rangement")])
def test_etape2_mise_a_l_ecart_physique(reel, cas):
    j = reel["journal"][cas]
    assert j["destination"].replace("\\", "/").startswith(MANIFESTE[cas]["rangement"] + "/")
    assert (reel["sortie"] / j["destination"]).is_file()


# ───────── Étape 3 : traitement réel ─────────

@pytest.mark.parametrize("cas", CAS)
def test_etape3_statut_final_et_sorties(reel, cas):
    j, att = reel["journal"][cas], MANIFESTE[cas]
    if att.get("doublon") and j["statut"] == "IGNOREE_DOUBLON":
        return
    assert j["statut"] == att["final"], j["motif"]
    produits = [s for s in reel["sorties"] if s["chemin_source"] == j["chemin_source"]]
    assert len(produits) == att["sorties"]
    centre = "Nantes" if cas.startswith("Nantes") else "Lille"
    for s in produits:
        f = reel["sortie"] / s["fichier_sortie"]
        p = Path(s["fichier_sortie"]).parts
        assert (p[:2], p[3]) == (("images_traitees", centre), att["id"])
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", p[2]), p  # images_traitees/<centre>/<date>/<id>/
        ds = pydicom.dcmread(str(f))
        assert ds.PatientID == att["id"] and str(ds.PatientName) == ""


def test_etape3_aucun_fichier_non_attendu_dans_images_traitees(reel):
    attendu = sum(a["sorties"] for c, a in MANIFESTE.items()
                  if not (a.get("doublon") and reel["journal"][c]["statut"] == "IGNOREE_DOUBLON"))
    assert len(list((reel["sortie"] / "images_traitees").rglob("*.dcm"))) == attendu


def test_etape3_originaux_intacts(reel):
    racine = reel["racine"]
    for rel, cas in MANIFESTE.items():
        if " :: " not in rel:
            assert (racine / rel).read_bytes() == cas["octets"]


# ───────── Étape 4 : rapport ─────────

def test_etape4_totaux_rapport(reel):
    attendus = Counter()
    for c, a in MANIFESTE.items():
        attendus[reel["journal"][c]["statut"] if a.get("doublon") else a["final"]] += 1
    assert reel["execution"]["statuts"] == dict(attendus)
    assert reel["execution"]["nb_dicom_produits"] == len(list((reel["sortie"] / "images_traitees").rglob("*.dcm")))


def test_etape4_rapport_signale_alertes(reel):
    md = (reel["sortie"] / "rapports" / "rapport_execution.md").read_text(encoding="utf-8")
    assert "incohérence(s) de métadonnées" in md and "Taux de rattachement" in md


# ───────── Étape 5 : confrontation ─────────

def _verdicts(ligne):
    out = {}
    for d in filter(None, ligne["detail_par_modalite"].split(" | ")):
        lib, _, reste = d.partition(":")
        out[lib] = reste.rsplit("-> ", 1)[1]
    return out


@pytest.mark.parametrize("mode", ["simu", "reel"])
@pytest.mark.parametrize("pid", sorted(CONFRONTATION))
def test_etape5_confrontation(request, mode, pid):
    res = request.getfixturevalue(mode)
    ligne = res["confrontation"][pid]
    att = CONFRONTATION[pid]
    assert ligne["statut_confrontation"] == att["statut"]
    if att["ecarts"] is None:
        return
    v = _verdicts(ligne)
    assert len(v) == 10                                    # FO×2, OCT-H×2, OCT-V×2, AF maculaire×2, AF grand champ×2
    ecarts = {k: x for k, x in v.items() if x not in ("OK", "NON_EVALUABLE_SANS_TRAITEMENT")}
    assert ecarts == att["ecarts"], ligne["detail_par_modalite"]
