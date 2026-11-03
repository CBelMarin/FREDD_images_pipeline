"""Reconnaissance des identifiants SKEZIA : tables de cas exhaustives.

Chaque ligne d'une table = un cas réel ou plausible de nommage. Ajouter un
cas = ajouter une ligne. Les cas marqués « COMPORTEMENT DOCUMENTÉ » ne sont
pas forcément souhaitables mais sont le comportement voulu et assumé : ils
échouent si quelqu'un le change sans le décider.
"""
import pytest

import pipeline_fredd as pf

U1 = "300b72a1-c067-400a-b148-0141a13f641d"
U1_AUTRE_FIN = "300b72a1-ffff-4fff-8fff-ffffffffffff"   # même préfixe, autre patient
U2 = "4c5d6e7f-1111-4222-8333-944455556666"


# ═════════════ 1. Lecture d'un ID en début de nom (un seul composant) ═════════════

@pytest.mark.parametrize("nom,attendu", [
    # formes conformes
    (U1, "300b72a1"),
    (U1.upper(), "300b72a1"),
    (f"{U1}_OCT", "300b72a1"),
    (f"{U1} OD", "300b72a1"),
    ("300b72a1", "300b72a1"),
    ("300B72A1", "300b72a1"),
    ("300b72a1_OD_fundus", "300b72a1"),
    ("300b72a1-OD", "300b72a1"),
    ("300b72a1.OD", "300b72a1"),
    ("300b72a1 (2)", "300b72a1"),
    ("300b72a1-c067", "300b72a1"),                 # UUID incomplet : préfixe seul
    ("  300b72a1_OD  ", "300b72a1"),               # espaces parasites
    ("00000001", "00000001"),                       # que des chiffres mais pas une date
    # non conformes -> rien
    ("20231005", ""),                               # date AAAAMMJJ
    ("20231005_OCT", ""),
    ("19991231", ""),
    ("300b72a12", ""),                              # 9 hexa : pas un ID tronqué
    ("300b72a", ""),                                # 7 hexa
    ("300b72a1c067400ab1480141a13f641d", ""),       # COMPORTEMENT DOCUMENTÉ : UUID sans tirets non reconnu
    ("OCT_300b72a1", ""),                           # pas en préfixe
    ("Patient 300b72a1", ""),
    ("divers", ""),
    ("", ""),
    ("OD", ""),
    ("300g72a1", ""),                               # 'g' n'est pas hexadécimal
    # COMPORTEMENT DOCUMENTÉ : un mot hexadécimal de 8 lettres est pris pour un ID ;
    # il sera ensuite écarté en SANS_PATIENT s'il n'est pas dans le CSV.
    ("deadbeef_x", "deadbeef"),
    ("20231399", "20231399"),                       # pas une date valide (mois 13)
])
def test_id_en_prefixe(nom, attendu):
    assert pf.id_en_prefixe(nom) == attendu


# ═════════════ 2. ID d'un chemin complet (dossiers + fichier, zips) ═════════════

R = "/racine"


@pytest.mark.parametrize("chemin,pid,discordant,obs", [
    # conformes
    (f"{R}/Lille/{U1}/{U1}_OD.e2e", "300b72a1", False, ""),
    (f"{R}/Lille/{U1}/300b72a1_OD.e2e", "300b72a1", False, ""),            # dossier complet, fichier tronqué
    (f"{R}/Lille/300b72a1/{U1}_OD.e2e", "300b72a1", False, ""),            # l'inverse
    (f"{R}/Lille/300B72A1/300b72a1_OD.e2e", "300b72a1", False, ""),        # casse différente : même ID
    (f"{R}/Lille/300b72a1/2023/300b72a1_OD.dcm", "300b72a1", False, ""),   # sous-dossier non-ID
    (f"{R}/Lille/300b72a1/20231005/300b72a1_OD.dcm", "300b72a1", False, ""),  # sous-dossier daté
    (f"{R}/Lille/300b72a1_OD.dcm", "300b72a1", False, ""),                 # fichier directement sous le centre
    (f"{R}/Lille.zip :: 300b72a1/x.zip :: 300b72a1_OG.e2e", "300b72a1", False, ""),  # zips imbriqués
    (f"{R}/Lille/annee_2022/300b72a1/300b72a1_OD.dcm", "300b72a1", False, ""),       # niveau intermédiaire
    # fichier non renommé : rattaché au dossier, signalé
    (f"{R}/Lille/300b72a1/IMG_0001.dcm", "300b72a1", False, "non renommé"),
    (f"{R}/Lille/300b72a1/OD/scan.e2e", "300b72a1", False, "non renommé"),
    # discordances : jamais devinées
    (f"{R}/Lille/300b72a1/4c5d6e7f_OD.dcm", "4c5d6e7f", True, "discordants"),
    (f"{R}/Lille/300b72a1/4c5d6e7f/x.dcm", "4c5d6e7f", True, "discordants"),          # deux dossiers ID
    (f"{R}/Lille.zip :: 300b72a1/4c5d6e7f_OG.dcm", "4c5d6e7f", True, "discordants"),
    # hors convention : repli signalé
    (f"{R}/Lille/divers/OCT_300b72a1.dcm", "300b72a1", False, "hors convention"),
    (f"{R}/Lille/Patient 300b72a1/scan.dcm", "300b72a1", False, "hors convention"),
    (f"{R}/Lille/divers/OCT_300b72a1_vs_4c5d6e7f.dcm", "300b72a1", True, "discordants"),
    # aucun ID
    (f"{R}/Lille/divers/DUPONT_JEAN.dcm", "", False, ""),
    (f"{R}/Lille/divers/20231005_OCT.dcm", "", False, ""),
    # le nom du CENTRE n'est jamais un ID, même hexadécimal
    (f"{R}/abcdef12/scan.dcm", "", False, ""),
    (f"{R}/300b72a1.zip :: scan.dcm", "", False, ""),
])
def test_pseudo_sous_centre(chemin, pid, discordant, obs):
    p, o, d = pf.pseudo_sous_centre(chemin, R)
    assert (p, d) == (pid, discordant), o
    assert obs in o
    if not obs:
        assert o == ""


def test_id_au_dessus_du_dossier_scanne_ignore():
    assert pf.pseudo_sous_centre(f"/{U1}/racine/Lille/divers/x.dcm", "/" + U1 + "/racine")[0] == ""


@pytest.mark.parametrize("chemin,attendus", [
    (f"{R}/Lille/{U1}/{U1}_OD.e2e", [U1]),
    (f"{R}/Lille/{U1}/{U1_AUTRE_FIN}_OD.e2e", [U1, U1_AUTRE_FIN]),
    (f"{R}/Lille/300b72a1/300b72a1_OD.e2e", []),
    (f"{R}/{U1}/x.dcm", []),                         # centre exclu
])
def test_uuids_sous_centre(chemin, attendus):
    assert pf.uuids_sous_centre(chemin, R) == attendus


# ═════════════ 3. Appariement avec le CSV FREDD ═════════════

def _donnees(tmp_path, contenu, **kw):
    p = tmp_path / "d.csv"
    p.write_text(contenu, encoding="utf-8")
    return pf.DonneesFredd.charger(p, **kw)


def _tri(donnees, chemin, categorie="DICOM", sha="h1"):
    ligne = dict(chemin=chemin, racine_scan=R, lot="racine/" + chemin[len(R) + 1:].split("/")[0].split(" ::")[0],
                 categorie=categorie, format_detecte=categorie, sha256=sha, nom_fichier=chemin.split("/")[-1],
                 patient_id_detecte="")
    return pf.etape2_tri([ligne], donnees)[0]


@pytest.mark.parametrize("csv_ids,chemin,statut,id_fredd", [
    # ID complet dans le CSV
    ([U1], f"{R}/Lille/300b72a1/300b72a1_OD.dcm", "A_TRAITER", U1),
    ([U1], f"{R}/Lille/{U1}/{U1}_OD.dcm", "A_TRAITER", U1),
    ([U1], f"{R}/Lille/{U1_AUTRE_FIN}/x_OD.dcm", "MISE_DE_COTE_ID_AMBIGU", ""),     # même préfixe, autre UUID
    ([U1], f"{R}/Lille/300b72a1/{U1_AUTRE_FIN}_OD.dcm", "MISE_DE_COTE_ID_AMBIGU", ""),
    # ID tronqué dans le CSV : la fin d'un UUID du chemin ne peut pas être vérifiée
    (["300b72a1"], f"{R}/Lille/{U1}/x.dcm", "A_TRAITER", "300b72a1"),
    # écritures variées du même ID dans le CSV
    ([U1.upper()], f"{R}/Lille/300b72a1/x.dcm", "A_TRAITER", U1),
    (["{" + U1 + "}"], f"{R}/Lille/300b72a1/x.dcm", "A_TRAITER", U1),
    (['"' + U1 + '"'], f"{R}/Lille/300b72a1/x.dcm", "A_TRAITER", U1),
    (["  " + U1 + "  "], f"{R}/Lille/300b72a1/x.dcm", "A_TRAITER", U1),
    # absent / ambigu / non détecté
    ([U2], f"{R}/Lille/300b72a1/x.dcm", "MISE_DE_COTE_SANS_PATIENT", ""),
    ([U1, U1_AUTRE_FIN], f"{R}/Lille/300b72a1/x.dcm", "MISE_DE_COTE_ID_AMBIGU", ""),  # collision dans le CSV
    ([U1], f"{R}/Lille/divers/scan.dcm", "MISE_DE_COTE_ID_NON_DETECTE", ""),
    ([U1, U2], f"{R}/Lille/300b72a1/4c5d6e7f_OD.dcm", "MISE_DE_COTE_ID_AMBIGU", ""),  # discordance
])
def test_appariement_csv(tmp_path, csv_ids, chemin, statut, id_fredd):
    d = _donnees(tmp_path, "id\n" + "\n".join(csv_ids) + "\n")
    r = _tri(d, chemin)
    assert (r["statut"], r["id_fredd"]) == (statut, id_fredd), r["observation"]


def test_format_non_supporte_pour_patient_connu(tmp_path):
    d = _donnees(tmp_path, f"id\n{U1}\n")
    r = _tri(d, f"{R}/Lille/300b72a1/photo.jpg", categorie="IMAGE_STANDARD")
    assert (r["statut"], r["id_fredd"]) == ("NON_TRAITEE_FORMAT", U1)


def test_meme_patient_ecritures_differentes_pas_ambigu(tmp_path):
    d = _donnees(tmp_path, f"id\n{U1}\n{U1.upper()}\n{{{U1}}}\n")
    assert not d.ambigus and d.lignes_fusionnees == 2


def test_visites_multiples_declaration_fusionnee(tmp_path):
    """Une image déclarée à la 2e ligne (visite) d'un patient doit compter."""
    d = _donnees(tmp_path, f"id;fo;oct\n{U1};0;\n{U1};1;\n{U1};0;0\n", cols_skezia="fo:fundus,oct:oct_bscan")
    p = d.patients["300b72a1"]
    assert pf.valeur_declaree(p["skezia"]["fo"]) is True
    assert pf.valeur_declaree(p["skezia"]["oct"]) is False   # vide puis 0 -> non déclaré (renseigné)


def test_mauvaise_colonne_id_signalee(tmp_path):
    """--col-id sur l'ID BaMaRa (numérique) : rien ne s'apparie ; il faut que
    cela se VOIE (compteur + avertissement dans le rapport)."""
    d = _donnees(tmp_path, "id;bamara\n%s;123456\n" % U1, col_id="bamara")
    assert d.ids_format_inattendu == 1


def test_doublons_meme_patient_garde_la_copie_utile(tmp_path):
    d = _donnees(tmp_path, f"id\n{U1}\n")
    lignes = [dict(chemin=c, racine_scan=R, lot="racine/Lille", categorie="DICOM", format_detecte="DICOM",
                   sha256="meme", nom_fichier="x", patient_id_detecte="")
              for c in (f"{R}/Lille/300b72a1/divers/x.dcm", f"{R}/Lille/300b72a1/300b72a1_x.dcm")]
    recs = pf.etape2_tri(lignes, d)
    assert sorted(r["statut"] for r in recs) == ["A_TRAITER", "IGNOREE_DOUBLON"]


def test_meme_contenu_rattache_a_deux_patients_signale(tmp_path):
    d = _donnees(tmp_path, f"id\n{U1}\n{U2}\n")
    lignes = [dict(chemin=c, racine_scan=R, lot="racine/Lille", categorie="DICOM", format_detecte="DICOM",
                   sha256="meme", nom_fichier="x", patient_id_detecte="")
              for c in (f"{R}/Lille/300b72a1/300b72a1_x.dcm", f"{R}/Lille/4c5d6e7f/4c5d6e7f_x.dcm")]
    recs = pf.etape2_tri(lignes, d)
    assert all(r["statut"] == "A_TRAITER" for r in recs)   # pas de dédoublonnage entre patients…
    assert all("identique à une image rattachée" in r["observation"] for r in recs)  # …mais signalé
