"""Tests de bout en bout du pipeline FREDD (avec un faux raretia conforme à la doc)."""
import json
from collections import Counter
from pathlib import Path

import pydicom
import pytest

from conftest import VRAI_IPP, VRAI_NOM, dossier_patient, empreinte_arbre, lire_csv


@pytest.fixture
def resultat(jeu, lancer):
    avant = empreinte_arbre(jeu["racine"])
    code = lancer(jeu)
    apres = empreinte_arbre(jeu["racine"])
    s = jeu["sortie"]
    journal = lire_csv(s / "rapports" / "journal_images.csv")
    par_nom = {}
    for r in journal:
        par_nom.setdefault(Path(r["chemin_source"].split(" :: ")[-1]).name, []).append(r)
    return {"code": code, "avant": avant, "apres": apres, "s": s, "journal": journal, "par_nom": par_nom}


def statut(res, nom):
    lignes = res["par_nom"][nom]
    assert len(lignes) == 1, f"{nom} : {len(lignes)} lignes"
    return lignes[0]["statut"]


# ───────────── Étape 1 : inventaire ─────────────

def test_code_retour_et_originaux_intacts(resultat):
    assert resultat["code"] == 0
    assert resultat["avant"] == resultat["apres"], "Les fichiers d'origine ont été modifiés !"


def test_chaque_image_a_exactement_une_ligne(resultat):
    noms = Counter(Path(r["chemin_source"].split(" :: ")[-1]).name for r in resultat["journal"])
    attendus = {"img1.dcm", "volume.e2e", "photo.jpg", "multi.dcm", "sombre.dcm", "inconnu.dcm",
                "DUPONT_JEAN.dcm", "fuite.dcm", "mauvais_centre.dcm", "img1_copie.dcm", "ok.dcm",
                "plante.dcm", "sanspixels.dcm", "sans_extension"}
    assert set(noms) == attendus
    assert all(n == 1 for n in noms.values())
    assert "notes.txt" not in noms


def test_erreur_zip_corrompu_tracee(resultat):
    inv_dir = resultat["s"] / "01_inventaire"
    erreurs = lire_csv(next(inv_dir.glob("erreurs_inventaire_*.csv")))
    assert any("abimee.zip" in e["chemin"] and e["type_erreur"] == "BadZipFile" for e in erreurs)


def test_centres(resultat):
    centres = {r["centre"] for r in resultat["journal"]}
    assert centres == {"CentreA", "CentreB"}  # 'CentreB.zip' -> 'CentreB'


# ───────────── Étape 2 : mise à l'écart ─────────────

def test_sans_patient(resultat):
    s = resultat["s"]
    assert statut(resultat, "inconnu.dcm") == "MISE_DE_COTE_SANS_PATIENT"
    assert (s / "sans_patient" / "CentreA" / "99999999" / "inconnu.dcm").is_file()
    assert statut(resultat, "DUPONT_JEAN.dcm") == "MISE_DE_COTE_ID_NON_DETECTE"
    assert (s / "sans_patient" / "CentreA" / "_id_non_detecte" / "divers" / "DUPONT_JEAN.dcm").is_file()
    # aucune image sans patient ne part dans images_traitees
    assert not (dossier_patient(s, "CentreA", "99999999")).exists()


def test_format_non_supporte_mis_de_cote(resultat):
    assert statut(resultat, "photo.jpg") == "NON_TRAITEE_FORMAT"
    assert list((resultat["s"] / "non_traitees_format" / "CentreA" / "a1b2c3d4").rglob("photo.jpg"))


def test_doublon_binaire_traite_une_seule_fois(resultat):
    st = sorted([statut(resultat, "img1.dcm"), statut(resultat, "img1_copie.dcm")])
    assert st == ["IGNOREE_DOUBLON", "TRAITEE_OK"]


# ───────────── Étape 3 : raretia + rangement par centre ─────────────

def test_traitement_et_rangement(resultat):
    s = resultat["s"]
    assert statut(resultat, "multi.dcm") == "TRAITEE_OK"
    assert statut(resultat, "volume.e2e") == "TRAITEE_OK"
    assert statut(resultat, "mauvais_centre.dcm") == "TRAITEE_OK"
    assert statut(resultat, "sans_extension") == "TRAITEE_OK"
    assert len(list((dossier_patient(s, "CentreA", "b2c3d4e5")).glob("*.dcm"))) == 3  # 2 frames + sombre
    oct_ = [p for p in (dossier_patient(s, "CentreA", "a1b2c3d4")).glob("*oct_bscan*.dcm")]
    assert len(oct_) == 3
    assert (dossier_patient(s, "CentreB", "e5f6a7b8")).is_dir()


def test_a_verifier(resultat):
    r = resultat["par_nom"]["sombre.dcm"][0]
    assert r["statut"] == "TRAITEE_QUALITE_LIMITEE"
    assert "sombre" in r["motif"].lower()
    assert r["score_min"] == "0.210"


def test_crash_isole_par_reessai_unitaire(resultat):
    s = resultat["s"]
    assert statut(resultat, "ok.dcm") == "TRAITEE_OK"
    assert statut(resultat, "plante.dcm") == "ECHEC_TRAITEMENT"
    assert statut(resultat, "sanspixels.dcm") == "ECHEC_TRAITEMENT"
    dossier_echec = s / "echecs_traitement" / "CentreB" / "e5f6a7b8"
    # Nom de DESTINATION neutre (<id_fredd>__<empreinte>.ext), jamais le nom
    # d'origine : un nom d'origine peut être identifiant (ex. un patronyme réel),
    # cas relevé sur des données réelles. Le fichier
    # copié ne doit donc jamais s'appeler littéralement "plante.dcm".
    assert not (dossier_echec / "plante.dcm").exists()
    assert not (dossier_echec / "sanspixels.dcm").exists()
    fichiers_echec = list(dossier_echec.glob("e5f6a7b8__*.dcm"))
    assert len(fichiers_echec) == 2  # plante.dcm + sanspixels.dcm, tous deux ECHEC_TRAITEMENT
    paquets = lire_csv(s / "rapports" / "paquets_raretia.csv")
    assert any(p["centre"] == "CentreB" and p["code_retour"] != "0" and p["reessai_unitaire"] for p in paquets)
    assert list((s / "rapports" / "journal_raretia").rglob("*.log"))


def test_pseudonymisation_des_sorties(resultat):
    s = resultat["s"]
    sorties = list((s / "images_traitees").rglob("*.dcm"))
    assert len(sorties) >= 10
    for p in sorties:
        ds = pydicom.dcmread(str(p))
        assert str(ds.PatientName) == ""
        id_dossier = p.parent.name
        assert ds.PatientID == id_dossier, "PatientID doit être l'identifiant FREDD"
        brut = p.read_bytes()
        assert VRAI_IPP.encode() not in brut and b"DUPONT" not in brut
    # aucun nom de fichier d'origine identifiant dans les sorties
    assert not [p for p in sorties if "DUPONT" in p.name]


def test_quarantaine_si_fuite(resultat):
    s = resultat["s"]
    assert statut(resultat, "fuite.dcm") == "ECHEC_PSEUDONYMISATION"
    q = list((s / "quarantaine_pseudonymisation" / "CentreA" / "f6a7b8c9").glob("*.dcm"))
    assert len(q) == 1
    assert not (dossier_patient(s, "CentreA", "f6a7b8c9")).exists()
    sorties = lire_csv(s / "rapports" / "sorties_traitees.csv")
    assert any(r["rangement"] == "quarantaine" and "PatientName" in r["pseudonymisation"] for r in sorties)


def test_pas_de_dossier_travail_residuel(resultat):
    assert not (resultat["s"] / "_travail").exists()


# ───────────── Étape 4 : rapports ─────────────

def test_rapports_presents_et_coherents(resultat):
    s = resultat["s"]
    rap = s / "rapports"
    for f in ("rapport_execution.md", "journal_images.csv", "sorties_traitees.csv", "bilan_par_centre.csv",
              "confrontation_patients.csv", "paquets_raretia.csv", "execution.json",
              "synthese_execution.csv", "actions_a_mener.csv"):
        assert (rap / f).is_file(), f
    bilan = {b["centre"]: b for b in lire_csv(rap / "bilan_par_centre.csv")}
    total = sum(int(b["nb_images_inventoriees"]) for b in bilan.values())
    assert total == len(resultat["journal"])
    ej = json.loads((rap / "execution.json").read_text(encoding="utf-8"))
    assert sum(ej["statuts"].values()) == len(resultat["journal"])
    assert ej["raretia"]["version"] == "fake-0.1" and ej["raretia"]["sha256_code"]
    md = (rap / "rapport_execution.md").read_text(encoding="utf-8")
    assert "quarantaine" in md and "MISE_DE_COTE_SANS_PATIENT" in md
    assert md.index("## Synthèse chiffrée") < md.index("## Actions à mener") < md.index("## 1. Inventaire")
    # le nombre de DICOM rangés = nombre de lignes 'images_traitees' de sorties_traitees.csv
    sorties = lire_csv(rap / "sorties_traitees.csv")
    nb_images_traitees = len([x for x in sorties if x["rangement"] == "images_traitees"])
    assert nb_images_traitees == len(list((s / "images_traitees").rglob("*.dcm")))

    synthese = {r["indicateur"]: r["valeur"] for r in lire_csv(rap / "synthese_execution.csv")}
    assert int(synthese["Nombre total d'images (DICOM) produites et rangées dans images_traitees/"]) \
        == nb_images_traitees
    assert "oct_bscan" in synthese["Images par modalité, telles que détectées à l'inventaire (toutes, traitées ou non)"]

    actions = lire_csv(rap / "actions_a_mener.csv")
    assert any(a["priorite"] == "BLOQUANT" and "quarantaine" in a["sujet"].lower() for a in actions)
    assert any(a["priorite"] == "A_VERIFIER" and "Échecs de traitement" in a["sujet"] for a in actions)


# ───────────── Étape 5 : confrontation ─────────────

def test_confrontation(resultat):
    conf = {r["id_fredd"]: r for r in lire_csv(resultat["s"] / "rapports" / "confrontation_patients.csv")}
    assert conf["a1b2c3d4"]["statut_confrontation"] == "SKEZIA_ET_FREDD"
    assert "oct_bscan: déclaré=oui / trouvées=1 (dont piles=1) / traitées=3 -> OK" in conf["a1b2c3d4"]["detail_par_modalite"]
    assert "autofluorescence OD=1" in conf["a1b2c3d4"]["contenu_trouve"]
    assert conf["b2c3d4e5"]["statut_confrontation"] == "FREDD_SEULEMENT"
    assert conf["c3d4e5f6"]["statut_confrontation"] == "SKEZIA_SEULEMENT"
    assert conf["d4e5f6a7"]["statut_confrontation"] == "NI_SKEZIA_NI_FREDD"
    assert conf["e5f6a7b8"]["statut_confrontation"] == "SKEZIA_ET_FREDD"
    assert conf["99999999"]["statut_confrontation"] == "IMAGES_HORS_DONNEES"
    assert conf["99999999"]["present_donnees_fredd"] == "Non"
    assert conf["a1b2c3d4"]["centre_coherent"] == "Oui"
    assert conf["a7b8c9d0"]["centre_coherent"] == "Non"
    # doublon non compté deux fois
    assert int(conf["a1b2c3d4"]["nb_images_inventoriees"]) == 3


def test_pas_de_pseudo_herite_au_dessus_du_dossier_scanne(tmp_path, lancer):
    """Régression : un dossier parent au nom hexadécimal/UUID, AU-DESSUS du
    dossier scanné, ne doit pas servir de pseudonyme."""
    from conftest import dicom_bytes, ecrire
    parent = tmp_path / "export_0c98c96d-2ae2-52c1-bda6-fa0f4ea702a6"
    racine = parent / "images"
    ecrire(racine / "CentreA" / "divers" / "sans_id.dcm", dicom_bytes())
    ecrire(racine / "CentreA" / "a1b2c3d4" / "ok.dcm", dicom_bytes(seed=3))
    data = tmp_path / "d.csv"
    data.write_text("id\na1b2c3d4\n0c98c96d\n", encoding="utf-8")
    jeu = {"racine": racine, "data": data, "sortie": tmp_path / "s"}
    import pipeline_fredd
    from conftest import FAKE_RARETIA
    assert pipeline_fredd.main([str(racine), "--data", str(data), "--sortie", str(jeu["sortie"]),
                                "--raretia-path", str(FAKE_RARETIA)]) == 0
    j = {Path(r["chemin_source"]).name: r for r in lire_csv(jeu["sortie"] / "rapports" / "journal_images.csv")}
    assert j["sans_id.dcm"]["statut"] == "MISE_DE_COTE_ID_NON_DETECTE"
    assert "ignoré" in j["sans_id.dcm"]["observation"]
    assert j["ok.dcm"]["statut"] == "TRAITEE_OK"


def test_contenu_e2e_inventorie(resultat):
    s = resultat["s"]
    lignes = lire_csv(s / "01_inventaire" / "contenu_images.csv")
    e2e = [l for l in lignes if l["chemin_source"].endswith("volume.e2e")]
    assert {(l["modalite"], l["lateralite"]) for l in e2e} == {("oct_bscan", "OD"), ("autofluorescence", "OD")}
    assert [l["orientation"] for l in e2e if l["modalite"] == "oct_bscan"] == ["volume, lignes horizontales"]
    texte = (s / "01_inventaire" / "contenu_images.csv").read_text(encoding="utf-8-sig")
    assert "DUPONT" not in texte.replace("DUPONT_JEAN.dcm", "")  # seul le NOM DE FICHIER piège apparaît
    assert "IPP987654321" not in texte
    j = resultat["par_nom"]["volume.e2e"][0]
    assert "oct_bscan OD volume, lignes horizontales ×3" in j["contenu_detecte"]


def test_confrontation_fonctionne_en_simulation(jeu, lancer):
    """La confrontation par modalité repose sur le contenu inventorié : elle
    doit fonctionner SANS raretia."""
    assert lancer(jeu, "--simulation") == 0
    conf = {r["id_fredd"]: r for r in lire_csv(jeu["sortie"] / "rapports" / "confrontation_patients.csv")}
    assert "oct_bscan: déclaré=oui / trouvées=1 (dont piles=1) -> OK" in conf["a1b2c3d4"]["detail_par_modalite"]


# ───────────── Garde-fous et modes ─────────────

def test_simulation_ne_copie_rien(jeu, lancer):
    avant = empreinte_arbre(jeu["racine"])
    assert lancer(jeu, "--simulation") == 0
    s = jeu["sortie"]
    assert not (s / "images_traitees").exists()
    assert not (s / "sans_patient").exists()
    journal = lire_csv(s / "rapports" / "journal_images.csv")
    assert Counter(r["statut"] for r in journal)["A_TRAITER_SIMULATION"] >= 10
    assert empreinte_arbre(jeu["racine"]) == avant


def test_reprise_ne_retraite_pas_et_refaire_tout_force(jeu, lancer):
    """Relancer sur le MÊME dossier de sortie : les images déjà traitées (ou déjà
    en échec) lors du 1er passage ne sont pas renvoyées à raretia -- seul
    --refaire-tout les retraite. La mise à l'écart (étape 2), elle, est toujours
    ré-évaluée : si un patient apparaît ensuite dans le CSV, l'image mise de côté
    doit être rattrapée au passage suivant."""
    assert lancer(jeu) == 0
    s = jeu["sortie"]
    etat1 = lire_csv(s / "rapports" / "etat_traitement.csv")
    assert {e["statut"] for e in etat1} >= {"TRAITEE_OK", "ECHEC_TRAITEMENT", "ECHEC_PSEUDONYMISATION"}
    paquets1 = lire_csv(s / "rapports" / "paquets_raretia.csv")
    assert len(paquets1) > 0

    # 2e passage, même sortie, mêmes sources : code 0 (plus de refus), et aucun
    # nouveau paquet raretia (tout ce qui était A_TRAITER a déjà un résultat).
    assert lancer(jeu) == 0
    paquets2 = lire_csv(s / "rapports" / "paquets_raretia.csv")
    assert len(paquets2) == 0
    journal2 = lire_csv(s / "rapports" / "journal_images.csv")
    par_nom2 = {Path(r["chemin_source"].split(" :: ")[-1]).name: r for r in journal2}
    assert par_nom2["ok.dcm"]["statut"] == "TRAITEE_OK"
    assert "reprise" in par_nom2["ok.dcm"]["observation"]
    # le compte global ne change pas (rien retraité, rien perdu)
    assert Counter(r["statut"] for r in journal2) == Counter(r["statut"] for r in lire_csv(
        s / "rapports" / "journal_images.csv"))

    # --refaire-tout : on force un vrai nouveau passage dans raretia
    assert lancer(jeu, "--refaire-tout") == 0
    paquets3 = lire_csv(s / "rapports" / "paquets_raretia.csv")
    assert len(paquets3) > 0
    journal3 = {Path(r["chemin_source"].split(" :: ")[-1]).name: r for r in
                lire_csv(s / "rapports" / "journal_images.csv")}
    assert "reprise" not in journal3["ok.dcm"]["observation"]


def test_reprise_rattrape_mise_a_l_ecart_si_donnees_mises_a_jour(jeu, lancer, tmp_path):
    """Contrairement au traitement raretia, la mise à l'écart (étape 2) n'est PAS
    figée par la reprise : si le CSV FREDD est mis à jour entre deux passages
    (nouveau patient ajouté), l'image jusque-là dans sans_patient/ doit être
    rattrapée sans --refaire-tout."""
    assert lancer(jeu) == 0
    s = jeu["sortie"]
    assert statut_journal(s, "inconnu.dcm") == "MISE_DE_COTE_SANS_PATIENT"

    # Le CSV est mis à jour : "99999999" devient un vrai patient FREDD de CentreA
    import csv as _csv
    lignes = list(_csv.DictReader(open(jeu["data"], encoding="utf-8"), delimiter=";"))
    champs = list(lignes[0].keys())
    lignes.append({**{c: "" for c in champs}, "id": "99999999", "centre": "Centre A"})
    with open(jeu["data"], "w", newline="", encoding="utf-8") as f:
        w = _csv.DictWriter(f, fieldnames=champs, delimiter=";")
        w.writeheader()
        w.writerows(lignes)

    assert lancer(jeu) == 0
    assert statut_journal(s, "inconnu.dcm") == "TRAITEE_OK"


def statut_journal(sortie, nom):
    journal = lire_csv(sortie / "rapports" / "journal_images.csv")
    lignes = [r for r in journal if Path(r["chemin_source"].split(" :: ")[-1]).name == nom]
    assert len(lignes) == 1
    return lignes[0]["statut"]


def test_refus_sortie_dans_dossier_scanne(jeu, lancer):
    assert lancer(jeu, sortie=jeu["racine"] / "sortie") == 2


def test_raretia_introuvable(jeu):
    import pipeline_fredd
    code = pipeline_fredd.main([str(jeu["racine"]), "--data", str(jeu["data"]), "--sortie", str(jeu["sortie"]),
                                "--raretia-path", str(jeu["tmp"] / "nexistepas")])
    assert code == 2


def test_colonne_inexistante(jeu, lancer):
    assert lancer(jeu, "--col-id", "pas_une_colonne") == 2


def test_paquets_de_petite_taille(jeu, lancer):
    """Le découpage en paquets ne doit rien changer au résultat."""
    assert lancer(jeu, "--taille-paquet", "2") == 0
    journal = lire_csv(jeu["sortie"] / "rapports" / "journal_images.csv")
    c = Counter(r["statut"] for r in journal)
    assert c["TRAITEE_OK"] == 6 and c["ECHEC_TRAITEMENT"] == 2 and c["TRAITEE_QUALITE_LIMITEE"] == 1


def test_sous_dossier_par_date_de_traitement(jeu, lancer):
    """Chaque passage range ses NOUVELLES images dans images_traitees/<centre>/<date>/ ;
    les images reprises d'un passage précédent restent dans leur dossier de date
    d'origine, sans copie en double."""
    from conftest import dicom_bytes, ecrire
    s = jeu["sortie"]
    assert lancer(jeu, "--date-traitement", "2026-10-01") == 0
    lot1 = sorted(p.relative_to(s).as_posix() for p in (s / "images_traitees").rglob("*.dcm"))
    assert lot1 and all(p.split("/")[2] == "2026-10-01" for p in lot1)

    # nouvel envoi : une image de plus pour un patient déjà traité
    ecrire(jeu["racine"] / "CentreA" / "a1b2c3d4" / "OG" / "nouvelle.dcm", dicom_bytes(seed=42))
    assert lancer(jeu, "--date-traitement", "2026-10-16") == 0
    tout = sorted(p.relative_to(s).as_posix() for p in (s / "images_traitees").rglob("*.dcm"))
    nouveaux = [p for p in tout if p not in lot1]
    assert set(lot1) <= set(tout)                                   # rien déplacé ni supprimé
    assert nouveaux and all(p.startswith("images_traitees/CentreA/2026-10-16/a1b2c3d4/") for p in nouveaux)
    sorties = lire_csv(s / "rapports" / "sorties_traitees.csv")
    assert len([x for x in sorties if x["rangement"] == "images_traitees"]) == len(tout)  # pas de doublon
    assert statut_journal(s, "nouvelle.dcm") == "TRAITEE_OK"
    rapport = (s / "rapports" / "rapport_execution.md").read_text(encoding="utf-8")
    assert f"images_traitees/<centre>/2026-10-16/` : **{len(nouveaux)}**" in rapport


def test_date_traitement_invalide_refusee(jeu, lancer):
    with pytest.raises(SystemExit):
        lancer(jeu, "--date-traitement", "16/10/2026")


def test_dossiers_en_liens_symboliques_suivis_sans_boucle(jeu, lancer, tmp_path):
    """Stockage monté par lien symbolique (Jupyter / environnement sécurisé) : un dossier-centre qui est un
    lien est parcouru ; un lien qui boucle sur son parent ne fait ni boucle infinie ni doublon."""
    import os
    from conftest import dicom_bytes, ecrire
    reel = tmp_path / "stockage_S" / "CentreA"
    ecrire(reel / "a1b2c3d4" / "OD" / "lien.dcm", dicom_bytes(seed=77))
    racine = tmp_path / "images_liens"
    racine.mkdir()
    try:
        os.symlink(reel, racine / "CentreA", target_is_directory=True)
        os.symlink(reel, reel / "boucle", target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("liens symboliques non autorisés sur ce système")
    s = tmp_path / "sortie_liens"
    assert lancer({**jeu, "racine": racine, "sortie": s}) == 0
    lignes = [r for r in lire_csv(s / "rapports" / "journal_images.csv") if r["chemin_source"].endswith("lien.dcm")]
    assert len(lignes) == 1 and lignes[0]["statut"] == "TRAITEE_OK"
    assert len(list((s / "images_traitees").rglob("*.dcm"))) == 1


def test_texte_incruste_declare_part_en_quarantaine(tmp_path, lancer):
    """Un DICOM source qui déclare du texte incrusté (BurnedInAnnotation = YES) ne va jamais
    dans images_traitees : quarantaine, statut ECHEC_PSEUDONYMISATION, motif explicite.
    Un DICOM qui déclare NO est traité normalement."""
    from conftest import dicom_bytes, ecrire, lire_csv
    racine = tmp_path / "images"
    ecrire(racine / "CentreA" / "a1b2c3d4" / "texte.dcm", dicom_bytes(seed=21, burned="YES"))
    ecrire(racine / "CentreA" / "a1b2c3d4" / "propre.dcm", dicom_bytes(seed=22, burned="NO"))
    data = tmp_path / "d.csv"
    data.write_text("id;centre;img_oct;img_retino\na1b2c3d4;Centre A;oui;oui\n", encoding="utf-8")
    jeu = {"racine": racine, "data": data, "sortie": tmp_path / "s"}
    assert lancer(jeu) == 0
    j = {Path(r["chemin_source"]).name: r for r in lire_csv(jeu["sortie"] / "rapports" / "journal_images.csv")}
    assert j["texte.dcm"]["statut"] == "ECHEC_PSEUDONYMISATION"
    assert "BurnedInAnnotation" in j["texte.dcm"]["motif"]
    assert j["propre.dcm"]["statut"] == "TRAITEE_OK"
    assert list((jeu["sortie"] / "quarantaine_pseudonymisation").rglob("*.dcm"))
    assert len(list((jeu["sortie"] / "images_traitees").rglob("*.dcm"))) == 1


def test_coupure_en_cours_de_route_la_reprise_ne_perd_que_le_paquet_en_cours(jeu, lancer, monkeypatch):
    """Coupure (session fermée, Ctrl+C) au milieu de l'étape 3 : l'état des paquets déjà rangés doit
    être sur disque, sinon la reprise retraiterait tout."""
    import pipeline_fredd
    vrai = pipeline_fredd.ra.run_raretia
    appels = {"n": 0}

    def coupe(*a, **k):
        appels["n"] += 1
        if appels["n"] == 3:           # les paquets 1 et 2 sont finis et rangés
            raise KeyboardInterrupt
        return vrai(*a, **k)

    monkeypatch.setattr(pipeline_fredd.ra, "run_raretia", coupe)
    with pytest.raises(KeyboardInterrupt):
        lancer(jeu, "--taille-paquet", "2")
    etat = lire_csv(jeu["sortie"] / "rapports" / "etat_traitement.csv")
    assert len(etat) >= 2, "l'état des paquets terminés doit être écrit avant la coupure"
    deja = {e["sha256"] for e in etat}

    monkeypatch.setattr(pipeline_fredd.ra, "run_raretia", vrai)
    assert lancer(jeu, "--taille-paquet", "2") == 0
    journal = lire_csv(jeu["sortie"] / "rapports" / "journal_images.csv")
    repris = [r for r in journal if "reprise" in r["observation"]]
    assert {r["sha256"] for r in repris} >= deja - {""} and len(repris) >= 2
    c = Counter(r["statut"] for r in journal)
    assert c["TRAITEE_OK"] == 6 and c["ECHEC_TRAITEMENT"] == 2 and c["TRAITEE_QUALITE_LIMITEE"] == 1
