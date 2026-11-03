"""l'état de reprise est indexé par (empreinte, patient), comme le dédoublonnage de l'étape 2."""
import csv
from pathlib import Path
from types import SimpleNamespace

import pipeline_fredd as pf

OPTS = SimpleNamespace(simulation=False, refaire_tout=False)


def _rec(pid, statut, sha="a" * 64, chemin=None):
    return {"sha256": sha, "patient_id_detecte": pid, "statut": statut, "id_fredd": pid, "centre": "C",
            "chemin_source": chemin or f"x/{pid}.dcm", "motif": "", "observation": ""}


def test_meme_fichier_pour_deux_patients_ne_partage_pas_l_etat(tmp_path):
    a, b = _rec("aaaaaaaa", "TRAITEE_OK"), _rec("bbbbbbbb", "ECHEC_TRAITEMENT")
    ligne = {"chemin_source": a["chemin_source"], "fichier_sortie": "images_traitees/f.dcm"}
    pf.sauvegarder_etat(tmp_path, [a, b], [ligne], {}, {}, OPTS, "t")
    etat, sorties = pf.lire_etat_precedent(tmp_path)
    assert len(etat) == 2                                          # une ligne par patient, jamais d'écrasement
    suivant = [_rec("aaaaaaaa", "A_TRAITER"), _rec("bbbbbbbb", "A_TRAITER")]
    pf.appliquer_reprise(suivant, etat, OPTS)
    assert [r["statut"] for r in suivant] == ["TRAITEE_OK", "ECHEC_TRAITEMENT"]
    assert pf._chercher_etat(sorties, suivant[0])[0]["fichier_sortie"] == "images_traitees/f.dcm"
    assert pf._chercher_etat(sorties, suivant[1]) is None          # rien d'hérité du patient A


def test_etat_ecrit_par_la_v1_11_0_reste_lisible(tmp_path):
    rap = tmp_path / "rapports"
    rap.mkdir()
    with open(rap / "etat_traitement.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["sha256", "statut", "id_fredd", "centre", "chemin_source", "motif", "date_traitement",
                    "version_pipeline"])
        w.writerow(["a" * 64, "TRAITEE_OK", "aaaaaaaa", "C", "x", "", "2026-10-18", "1.11.0"])
    etat, _ = pf.lire_etat_precedent(tmp_path)
    r = _rec("aaaaaaaa", "A_TRAITER")
    pf.appliquer_reprise([r], etat, OPTS)
    assert r["statut"] == "TRAITEE_OK"
