"""Tirage d'un échantillon de patients par centre + traitement limité à cet échantillon."""
import csv
import json
from collections import Counter
from pathlib import Path

import pytest

import echantillon as ech
import fredd
from conftest import lire_csv
from test_fredd_cli import _config


def _contenu(path, lignes):
    """lignes : (centre, id_fredd, categorie, modalite[, statut_tri[, erreur]])"""
    entete = ["chemin_source", "centre", "patient_id_detecte", "id_fredd", "statut_tri", "categorie",
              "serie", "modalite", "erreur"]
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(entete)
        for i, l in enumerate(lignes):
            c, pid, cat, mod = l[:4]
            statut = l[4] if len(l) > 4 else "A_TRAITER"
            err = l[5] if len(l) > 5 else ""
            w.writerow([f"/secret/NOM_PATIENT_{i}.dcm", c, pid[:8], pid, statut, cat, "1", mod, err])


def _jeu_inventaire(tmp_path):
    lignes = []
    for i in range(30):                                   # centre A : 30 patients en fond d'œil DICOM
        lignes.append(("A", f"a{i:07d}", "DICOM", "fundus"))
    for i in range(3):                                    # 3 patients A avec E2E OCT (rare)
        lignes.append(("A", f"e{i:07d}", "E2E_HEIDELBERG", "oct_bscan"))
    for i in range(2):                                    # 2 patients A avec Raw Data (rare)
        lignes.append(("A", f"r{i:07d}", "DICOM", "raw_data"))
    for i in range(20):                                   # centre B : 20 patients OCT DICOM seulement
        lignes.append(("B", f"b{i:07d}", "DICOM", "oct_bscan"))
    lignes.append(("A", "", "DICOM", "fundus", "MISE_DE_COTE_SANS_PATIENT"))      # sans patient : écarté
    lignes.append(("A", "z0000000", "DICOM", "fundus", "IGNOREE_DOUBLON"))        # doublon : écarté
    lignes.append(("A", "y0000000", "DICOM", "fundus", "A_TRAITER", "illisible"))  # illisible : écarté
    _contenu(tmp_path / "sortie" / "01_inventaire" / "contenu_images.csv", lignes)
    return tmp_path / "sortie"


def test_couverture_des_types_rares_et_effectif(tmp_path):
    sortie = _jeu_inventaire(tmp_path)
    assert ech.echantillonner(sortie, patients_par_centre=10, min_par_type=2, graine=7, log=lambda *_: None,
                              modalites_exclues=()) == 0
    pats = lire_csv(sortie / "echantillon" / "echantillon_patients.csv")
    par_centre = Counter(p["centre"] for p in pats)
    assert par_centre == {"A": 10, "B": 10}
    # strates rares présentes avec au moins 2 patients
    a = [p for p in pats if p["centre"] == "A"]
    assert sum("E2E_HEIDELBERG:oct_bscan" in p["types"] for p in a) >= 2
    assert sum("DICOM:raw_data" in p["types"] for p in a) >= 2
    # jamais de patient écarté (sans patient, doublon, illisible)
    ids = {p["id_fredd"] for p in pats}
    assert "" not in ids and "z0000000" not in ids and "y0000000" not in ids
    eff = lire_csv(sortie / "echantillon" / "echantillon_effectifs.csv")
    assert all(e["strate_sous_le_minimum"] == "non" for e in eff)
    rare = next(e for e in eff if e["centre"] == "A" and e["type"] == "E2E_HEIDELBERG:oct_bscan")
    assert rare["patients_disponibles"] == "3" and int(rare["patients_retenus"]) >= 2
    assert int(rare["images_retenues"]) == int(rare["patients_retenus"])      # une image par patient dans ce jeu


def test_reproductible_avec_la_meme_graine_et_graine_notee(tmp_path):
    sortie = _jeu_inventaire(tmp_path)
    ech.echantillonner(sortie, 10, 2, 123, log=lambda *_: None)
    p1 = (sortie / "echantillon" / "echantillon_patients.csv").read_text(encoding="utf-8-sig")
    ech.echantillonner(sortie, 10, 2, 123, log=lambda *_: None)
    assert (sortie / "echantillon" / "echantillon_patients.csv").read_text(encoding="utf-8-sig") == p1
    ech.echantillonner(sortie, 10, 2, 124, log=lambda *_: None)
    assert (sortie / "echantillon" / "echantillon_patients.csv").read_text(encoding="utf-8-sig") != p1
    params = json.loads((sortie / "echantillon" / "echantillon_parametres.json").read_text(encoding="utf-8"))
    assert params["graine"] == 124 and params["min_par_type"] == 2 and len(params["inventaire_sha256"]) == 64


def test_couverture_depasse_l_effectif_demande_et_strate_trop_petite(tmp_path):
    sortie = _jeu_inventaire(tmp_path)
    # 3 patients visés mais 3 types en A, minimum 3 : la couverture impose plus que 3 patients
    ech.echantillonner(sortie, 3, 3, 1, log=lambda *_: None, modalites_exclues=())
    pats = lire_csv(sortie / "echantillon" / "echantillon_patients.csv")
    a = [p for p in pats if p["centre"] == "A"]
    assert len(a) > 3                                    # tous les patients de couverture sont gardés
    eff = lire_csv(sortie / "echantillon" / "echantillon_effectifs.csv")
    raw = next(e for e in eff if e["centre"] == "A" and e["type"] == "DICOM:raw_data")
    assert raw["patients_disponibles"] == "2" and raw["patients_retenus"] == "2"   # moins que le minimum : tous
    assert raw["strate_sous_le_minimum"] == "oui"


def test_modalites_exclues_par_defaut_ne_servent_pas_a_la_couverture(tmp_path):
    sortie = _jeu_inventaire(tmp_path)
    logs = []
    assert ech.echantillonner(sortie, 5, 2, 7, log=logs.append) == 0           # défaut : raw_data exclue
    eff = lire_csv(sortie / "echantillon" / "echantillon_effectifs.csv")
    assert not any(e["type"].endswith(":raw_data") for e in eff)
    pats = lire_csv(sortie / "echantillon" / "echantillon_patients.csv")
    assert not any(p["id_fredd"].startswith("r") for p in pats)
    assert any("Modalités exclues de l'échantillon : infrarouge, raw_data" in l for l in logs)
    assert any(l.startswith("Images de l'échantillon : ") for l in logs)
    params = json.loads((sortie / "echantillon" / "echantillon_parametres.json").read_text(encoding="utf-8"))
    assert "raw_data" in params["modalites_exclues"] and params["nb_images"] > 0


def test_option_exclure_modalites_en_ligne_de_commande(tmp_path):
    sortie = _jeu_inventaire(tmp_path)
    base = ["--sortie", str(sortie), "--patients-par-centre", "5", "--min-par-type", "2", "--graine", "1"]
    assert ech.main(base + ["--exclure-modalites", "oct_bscan"]) == 0
    eff = lire_csv(sortie / "echantillon" / "echantillon_effectifs.csv")
    assert not any(e["type"].endswith(":oct_bscan") for e in eff)
    assert ech.main(base + ["--exclure-modalites", ""]) == 0
    eff = lire_csv(sortie / "echantillon" / "echantillon_effectifs.csv")
    assert any(e["type"].endswith(":raw_data") for e in eff)


def test_aucune_donnee_identifiante_dans_les_sorties(tmp_path):
    sortie = _jeu_inventaire(tmp_path)
    ech.echantillonner(sortie, 10, 2, 7, log=lambda *_: None)
    for f in (sortie / "echantillon").iterdir():
        texte = f.read_text(encoding="utf-8-sig")
        assert "NOM_PATIENT" not in texte and "/secret/" not in texte


def test_inventaire_absent_ou_parametres_invalides(tmp_path):
    assert ech.echantillonner(tmp_path / "vide", 10, 2, 1, log=lambda *_: None) == 2
    sortie = _jeu_inventaire(tmp_path)
    assert ech.echantillonner(sortie, 0, 2, 1, log=lambda *_: None) == 2


def test_lire_echantillon(tmp_path):
    f = tmp_path / "e.csv"
    f.write_text("centre;id_fredd;raison;types\nA;x1;aleatoire;t\nA;x2;couverture;t\n", encoding="utf-8")
    assert ech.lire_echantillon(f) == {"x1", "x2"}
    f.write_text("a;b\n1;2\n", encoding="utf-8")
    with pytest.raises(ValueError):
        ech.lire_echantillon(f)


# ───────────────────────── pipeline : traiter seulement l'échantillon ─────────────────────────

def test_pipeline_ne_traite_que_l_echantillon_puis_reprise_sans_echantillon(jeu, lancer, tmp_path):
    liste = tmp_path / "echantillon_patients.csv"
    liste.write_text("centre;id_fredd;raison;types\nCentre A;a1b2c3d4;aleatoire;x\n", encoding="utf-8")
    assert lancer(jeu, "--echantillon", str(liste)) == 0
    s = jeu["sortie"]
    journal = lire_csv(s / "rapports" / "journal_images.csv")
    par_statut = Counter(r["statut"] for r in journal)
    assert par_statut["HORS_ECHANTILLON"] >= 3                       # patients FREDD hors liste
    assert par_statut["MISE_DE_COTE_SANS_PATIENT"] >= 1              # la mise à l'écart est conservée
    traitees = {r["id_fredd"] for r in journal if r["statut"].startswith("TRAITEE")}
    assert traitees == {"a1b2c3d4"}
    # HORS_ECHANTILLON n'est pas mémorisé : sans l'option, ces images sont traitées au passage suivant
    assert all(e["statut"] != "HORS_ECHANTILLON" for e in lire_csv(s / "rapports" / "etat_traitement.csv"))
    assert lancer(jeu) == 0
    journal2 = lire_csv(s / "rapports" / "journal_images.csv")
    assert Counter(r["statut"] for r in journal2)["HORS_ECHANTILLON"] == 0
    assert {r["id_fredd"] for r in journal2 if r["statut"].startswith("TRAITEE")} > {"a1b2c3d4"}


def test_pipeline_echantillon_illisible(jeu, lancer, tmp_path):
    assert lancer(jeu, "--echantillon", str(tmp_path / "absent.csv")) == 2


# ───────────────────────── fredd.py ─────────────────────────

def test_config_echantillon_transmis_au_pipeline(tmp_path):
    cfg = fredd.lire_config(_config(tmp_path))
    assert "--echantillon" not in fredd.arguments_pipeline(cfg)
    texte = (tmp_path / "config_fredd.ini").read_text(encoding="utf-8")
    (tmp_path / "config_fredd.ini").write_text(
        texte.replace("taille_paquet = 50", f"taille_paquet = 50\nechantillon = {tmp_path / 'e.csv'}"), encoding="utf-8")
    a = fredd.arguments_pipeline(fredd.lire_config(tmp_path / "config_fredd.ini"))
    assert a[a.index("--echantillon") + 1].endswith("e.csv")


def test_commande_echantillon_utilise_l_inventaire_le_plus_recent(tmp_path, monkeypatch, capsys):
    cfgp = _config(tmp_path)
    monkeypatch.setattr(fredd, "CONFIG", cfgp)
    assert fredd.echantillon(["--patients-par-centre", "5", "--min-par-type", "1", "--graine", "1"]) == 2  # aucun inventaire
    assert fredd.simulation(cfgp) == 0
    assert fredd.echantillon(["--patients-par-centre", "5", "--min-par-type", "1", "--graine", "1"]) == 0
    assert (tmp_path / "sortie_simulation" / "echantillon" / "echantillon_patients.csv").is_file()
    assert "Inventaire utilisé" in capsys.readouterr().out
