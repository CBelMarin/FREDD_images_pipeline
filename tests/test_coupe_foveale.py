# -*- coding: utf-8 -*-
"""Vérifie le repérage de la coupe fovéale sur des volumes OCT synthétiques."""

import numpy as np
import pytest

import coupe_foveale as cf


def bscan(profondeur_creux, hauteur=496, largeur=1024, y_ilm=160, epaisseur=120, bruit=4.0, graine=0):
    """B-scan synthétique : bande rétinienne claire sous une ILM creusée au centre."""
    rng = np.random.default_rng(graine)
    x = np.arange(largeur)
    centre = largeur / 2
    # creux gaussien + léger bombement des épaulements, comme un profil maculaire
    creux = profondeur_creux * np.exp(-((x - centre) ** 2) / (2 * (largeur / 18) ** 2))
    epaule = -0.25 * profondeur_creux * np.exp(-((np.abs(x - centre) - largeur / 9) ** 2) / (2 * (largeur / 20) ** 2))
    ilm = y_ilm + creux + epaule

    img = rng.normal(12, bruit, (hauteur, largeur)).astype(np.float32)
    lignes = np.arange(hauteur)[:, None]
    dans_retine = (lignes >= ilm[None, :]) & (lignes < (ilm + epaisseur)[None, :])
    img[dans_retine] += 170
    return np.clip(img, 0, 255)


def volume(n_coupes, index_foveal, creux_max=45.0):
    """Volume dont la coupe `index_foveal` porte le creux le plus marqué."""
    out = []
    for i in range(n_coupes):
        attenuation = max(0.0, 1 - abs(i - index_foveal) / (n_coupes / 2.5))
        out.append(bscan(creux_max * attenuation, graine=i))
    return out


@pytest.mark.parametrize("n_coupes,index_foveal", [(25, 12), (49, 20), (9, 4), (97, 61)])
def test_coupe_foveale_identifiee(n_coupes, index_foveal):
    profils = [cf.profil_ilm(b) for b in volume(n_coupes, index_foveal)]
    res = cf.choisir_coupe(profils)
    assert res["methode"] == "creux_fovelaire"
    assert res["confiance"] != "indeterminee"
    # tolérance d'une coupe : les voisines immédiates ont un creux presque aussi profond
    assert abs(res["index"] - index_foveal) <= 1


def test_profil_ilm_suit_le_creux():
    img = bscan(50.0)
    p = cf.profil_ilm(img)
    largeur = img.shape[1]
    bord = float(np.nanmedian(p[:largeur // 10]))
    centre = float(np.nanmedian(p[largeur // 2 - 20: largeur // 2 + 20]))
    assert centre > bord + 25, "le creux central doit être nettement plus bas que les bords"


def test_volume_sans_creux_declare_indetermine():
    """Atrophie maculaire : plus de creux du tout -> la coupe ne doit pas être inventée."""
    profils = [cf.profil_ilm(bscan(0.0, graine=i)) for i in range(25)]
    res = cf.choisir_coupe(profils)
    assert res["confiance"] == "indeterminee"
    assert res["methode"] == "coupe_mediane_par_defaut"
    assert res["index"] == 12


def test_creux_hors_zone_centrale_ignore():
    """Un creux franc mais excentré (artefact de bord) ne doit pas être pris pour la fovéa."""
    img = bscan(0.0)
    img[:, :60] = 12.0  # trou noir sur le bord gauche
    prom, _ = cf.proeminence_creux(cf.profil_ilm(img))
    assert prom < cf.SEUIL_PROEMINENCE


def test_serie_de_deux_coupes_sans_objet(tmp_path):
    """Les lignes horizontale/verticale demandées par FREDD ne sont pas des volumes."""
    assert cf.MIN_COUPES_VOLUME > 2
