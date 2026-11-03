import csv

import analyse_patients as ap

ENTETE = ["id_fredd", "centre", "statut"]


def _journal(tmp_path, lignes):
    p = tmp_path / "journal_images.csv"
    with open(p, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(ENTETE)
        w.writerows(lignes)
    return p


def test_seules_les_images_exploitables_sont_comptees(tmp_path):
    lignes = ([["p1", "A", "A_TRAITER_SIMULATION"]] * 3
              + [["p1", "A", "IGNOREE_DOUBLON"], ["p1", "A", "NON_TRAITEE_RAW_DATA"], ["p1", "A", "NON_TRAITEE_IR"]]
              + [["p2", "A", "TRAITEE_OK"], ["p2", "A", "TRAITEE_QUALITE_LIMITEE"]]
              + [["p3", "A", "NON_TRAITEE_RAW_DATA"]]
              + [["", "A", "MISE_DE_COTE_SANS_PATIENT"]]
              + [["q1", "B", "A_TRAITER_SIMULATION"]] * 8)
    res = ap.analyser(_journal(tmp_path, lignes))
    assert sorted(res["A"]) == [2, 3]
    assert res["B"] == [8]
    r = ap.resume(res["A"])
    assert r["patients"] == 2 and r["images"] == 5 and r["moyenne"] == "2.5" and r["mediane"] == "2.5"


def test_sortie_sans_identifiant(tmp_path):
    lignes = [["pseudo-secret-123", "A", "A_TRAITER"]]
    sortie = []
    ap.afficher(ap.analyser(_journal(tmp_path, lignes)), log=sortie.append)
    assert "secret" not in "\n".join(sortie)


def test_journal_introuvable(tmp_path):
    assert ap.main([str(tmp_path)]) == 2
