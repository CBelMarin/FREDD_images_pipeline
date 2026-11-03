# -*- coding: utf-8 -*-
"""Un fichier source dont une partie seulement des sorties est en quarantaine.

Cas réel : 19 fichiers sources portaient le statut
ECHEC_PSEUDONYMISATION et une destination « quarantaine », alors que sur les
559 images qu'ils avaient produites, 35 seulement avaient été écartées et 524
avaient bien été livrées. Le statut au niveau fichier disait donc l'inverse de
ce qui s'était passé pour 94 % de ses images.
"""

import pipeline_fredd as pf


def sortie(pseudo="OK", acceptee=True):
    return {"pseudonymisation": pseudo, "acceptee_bool": acceptee}


def test_toutes_les_sorties_en_echec():
    statut = pf._statut_depuis_sorties([sortie("ECHEC"), sortie("ECHEC")])
    assert statut == "ECHEC_PSEUDONYMISATION"


def test_une_seule_sortie_en_echec_sur_un_volume():
    """49 B-scans, un seul porte le bandeau : le fichier n'est pas « en quarantaine »."""
    sorties = [sortie("ECHEC")] + [sortie("OK") for _ in range(48)]
    assert pf._statut_depuis_sorties(sorties) == "QUARANTAINE_PARTIELLE"


def test_aucune_sortie_en_echec():
    assert pf._statut_depuis_sorties([sortie(), sortie()]) == "TRAITEE_OK"
    assert pf._statut_depuis_sorties([sortie(), sortie(acceptee=False)]) == "TRAITEE_QUALITE_LIMITEE"
    assert pf._statut_depuis_sorties([sortie(acceptee=None)]) == "TRAITEE_QUALITE_INCONNUE"


def test_sans_sortie():
    assert pf._statut_depuis_sorties([]) == "ECHEC_TRAITEMENT"


def test_statut_declare_et_repris():
    """Le nouveau statut doit être décrit, compté comme traité, et repris sans retraitement."""
    assert "QUARANTAINE_PARTIELLE" in pf.STATUTS
    assert "QUARANTAINE_PARTIELLE" in pf.STATUTS_TRAITES
    assert "QUARANTAINE_PARTIELLE" in pf.STATUTS_REPRISE


def test_colonnes_de_comptage_presentes():
    for colonne in ("nb_sorties", "nb_sorties_livrees", "nb_sorties_quarantaine"):
        assert colonne in pf.JOURNAL_HEADERS
