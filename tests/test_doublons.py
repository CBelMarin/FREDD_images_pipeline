import csv

import analyse_doublons as ad

ENTETE = ["chemin_source", "racine_scan", "centre", "statut", "motif"]


def _journal(tmp_path, lignes):
    p = tmp_path / "journal_images.csv"
    with open(p, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(ENTETE)
        w.writerows(lignes)
    return p


def test_classement_des_doublons(tmp_path):
    r = "/data/images"
    lignes = [
        [f"{r}/transfert_A/p1/a.dcm", r, "A", "IGNOREE_DOUBLON", f"Identique à : {r}/transfert_A/p1/b.dcm"],
        [f"{r}/transfert_A/p1/c.dcm", r, "A", "IGNOREE_DOUBLON", f"Identique à : {r}/transfert_A/p2/c.dcm"],
        [f"{r}/transfert_A/p1/d.dcm", r, "A", "IGNOREE_DOUBLON", f"Identique à : {r}/transfert_B/p1/d.dcm"],
        [f"{r}/transfert_A/x.zip :: p1/e.dcm", r, "A", "IGNOREE_DOUBLON", f"Identique à : {r}/transfert_A/p1/e.dcm"],
        [f"{r}/transfert_A/p1/f.dcm", r, "A", "IGNOREE_DOUBLON", "Identique à : /ailleurs/f.dcm"],
        [f"{r}/transfert_A/p1/g.dcm", r, "A", "A_TRAITER", ""],
        [f"{r}/transfert_B/p9/h.dcm", r, "B", "IGNOREE_DOUBLON", "motif illisible"],
    ]
    res = ad.analyser(_journal(tmp_path, lignes))
    a, b = res["A"], res["B"]
    assert a["images"] == 6 and a["total"] == 5
    assert a["meme_dossier"] == 1
    assert a["meme_transfert"] == 2  # p1/p2 et zip/dossier du même transfert
    assert a["transferts_differents"] == 1
    assert a["autre_racine"] == 1
    assert a["zip"] == 1
    assert b["total"] == 1 and b["sans_copie_gardee"] == 1


def test_la_sortie_ne_contient_aucun_chemin(tmp_path):
    r = "/data/images"
    lignes = [[f"{r}/transfert_SECRET/DUPONT_JEAN/a.dcm", r, "A", "IGNOREE_DOUBLON",
               f"Identique à : {r}/transfert_SECRET/DUPONT_JEAN/b.dcm"]]
    sortie = []
    ad.afficher(ad.analyser(_journal(tmp_path, lignes)), log=sortie.append)
    texte = "\n".join(sortie)
    assert "DUPONT" not in texte and "SECRET" not in texte and "dcm" not in texte


def test_journal_introuvable(tmp_path, capsys):
    assert ad.main([str(tmp_path)]) == 2
