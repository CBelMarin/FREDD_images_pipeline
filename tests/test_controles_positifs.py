# -*- coding: utf-8 -*-
"""Le banc de contrôles positifs doit produire des positifs réellement détectables.

Si un bandeau blanc franc n'était pas retrouvé par le détecteur, ce serait le banc
qui serait en cause, pas le détecteur : ce test protège la mesure elle-même.
"""

import numpy as np

import controle_texte as ct
import controles_positifs as cp


def fond(h=496, w=1024, valeur=18):
    rng = np.random.default_rng(0)
    return np.clip(rng.normal(valeur, 3, (h, w)), 0, 255).astype(np.uint8)


def test_bandeau_blanc_est_detecte():
    img = cp._incruster(fond(), cp.NIVEAUX["blanc"], cp.TAILLES["moyenne"], "bande_haute", "marge_sombre")
    assert ct.lignes_suspectes(img) >= ct.SEUIL_LIGNES


def test_image_sans_texte_nest_pas_signalee():
    assert ct.lignes_suspectes(fond()) < ct.SEUIL_LIGNES


def test_le_gris_moyen_est_le_point_aveugle_attendu():
    """Documente la limite annoncée : un gris intermédiaire passe sous les seuils."""
    img = cp._incruster(fond(valeur=90), cp.NIVEAUX["gris_moyen"], cp.TAILLES["moyenne"],
                        "bande_haute", "marge_sombre")
    assert ct.lignes_suspectes(img) < ct.SEUIL_LIGNES


def test_incruster_ne_modifie_pas_la_source():
    src = fond()
    avant = src.copy()
    cp._incruster(src, 255, 0.75, "bande_basse", "sur_retine")
    assert np.array_equal(src, avant)


def test_valeurs_du_bandeau_sont_fictives():
    joint = " ".join(cp.LIGNES_BANDEAU)
    assert "DUPONT" in joint and "000000" in joint


def test_toutes_les_conditions_sont_couvertes():
    assert set(cp.NIVEAUX) >= {"blanc", "noir", "gris_moyen"}
    assert len(cp.TAILLES) >= 3 and len(cp.POSITIONS) >= 3 and len(cp.FONDS) == 2
