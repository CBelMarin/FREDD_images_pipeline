# -*- coding: utf-8 -*-
"""
figure1_pipeline.py : dessine la Figure 1 de l'article (architecture du pipeline en cinq etapes).

Usage :  python figure1_pipeline.py [--sortie figure1_pipeline.png]
Necessite seulement matplotlib. Les coordonnees sont en pixels d'une toile de 2000 x 1135 (origine en haut a gauche).
"""
import argparse

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch, Polygon

W, H, DPI = 2000, 1190, 300     # toile (unites), resolution ; la figure fait 6,3 pouces de large (largeur imprimee)
LARGEUR_POUCES = 6.3
POLICE = "Liberation Sans"
BLEU, VERT, ORANGE, ROUGE, GRIS = "#2563eb", "#0f766e", "#b45309", "#b91c1c", "#9ca3af"
FLECHE, TEXTE = "#6b7280", "#1f2937"

ETAPES = [  # (x0, couleur, titre, lignes)
    (38, BLEU, "1  Inventory", ["storage scan", "nested zip archives", "SHA-256 per file", "DICOM / E2E / other"]),
    (422, BLEU, "2  Analysis", ["header reading", "modality, eye, OCT", "orientation, device", "montage, angiography", "detection"]),
    (806, BLEU, "3  Sorting", ["pseudonym", "(from path)", "FREDD patient match", "deduplication", "(SHA-256, patient)", "set-aside rules"]),
    (1190, VERT, "4  Preprocessing", ["external module", "cropping, CLAHE,", "resizing,", "pseudonymized", "DICOM, quality,", "field of view"]),
    (1574, ORANGE, "5  Reports", ["eCRF reconciliation", "image catalog", "corrections per center", "run report"]),
]
GARDE_FOUS = ["no name or identifier   |   forbidden DICOM tags empty   |   no source identifier in pixels or tags",
              "embedded-annotation indicator   |   pixel text detector (no OCR)   |   study UID remapped",
              "infrared and angiography excluded"]
SORTIES = [  # (x0, x1, couleur, lignes)
    (38, 413, VERT, ["processed images", "(center / date / ID)"]),
    (417, 791, ROUGE, ["quarantine", "(pseudonymization,", "text)"]),
    (795, 1169, ORANGE, ["set aside", "(unknown or", "ambiguous ID)"]),
    (1173, 1548, GRIS, ["not processed", "(raw data, IR,", "montage, angiography,", "other)"]),
    (1552, 1927, GRIS, ["processing failures"]),
]


def boite(ax, x0, y0, x1, y1, bord, fond="white", lw=1.4, rayon=20, style="-"):
    ax.add_patch(FancyBboxPatch((x0, y0), x1 - x0, y1 - y0, boxstyle=f"round,pad=0,rounding_size={rayon}",
                                fc=fond, ec=bord, lw=lw, ls=style))


def lignes(ax, x, y0, textes, pas, taille, couleur=TEXTE, **kw):
    for k, t in enumerate(textes):
        ax.text(x, y0 + k * pas, t, ha="center", va="center", fontsize=taille, color=couleur, **kw)


def main(sortie="figure1_pipeline.png"):
    fig = plt.figure(figsize=(LARGEUR_POUCES, LARGEUR_POUCES * H / W), dpi=DPI)
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_xlim(0, W)
    ax.set_ylim(H, 0)
    ax.axis("off")
    plt.rcParams["font.family"] = [POLICE, "DejaVu Sans"]

    for i, (x0, c, titre, lg) in enumerate(ETAPES):
        boite(ax, x0, 96, x0 + 349, 535, c)
        ax.text(x0 + 174, 161, titre, ha="center", va="center", fontsize=9.0, fontweight="bold", color=c)
        lignes(ax, x0 + 174, 240, lg, 38, 7.4)
        if i < len(ETAPES) - 1:   # petit triangle gris entre deux etapes
            xt = x0 + 349 + 5
            ax.add_patch(Polygon([(xt, 306), (xt, 326), (xt + 20, 316)], fc=FLECHE, ec="none"))
    # fleche rouge de l'etape 4 vers les garde-fous
    ax.plot([1364, 1364], [541, 596], color=ROUGE, lw=1.3)
    ax.add_patch(Polygon([(1354, 594), (1374, 594), (1364, 613)], fc=ROUGE, ec="none"))

    boite(ax, 38, 629, 1922, 830, ROUGE, fond="#fef2f2")
    ax.text(980, 669, "Independent safeguards after step 4 (quarantine on failure)", ha="center", va="center",
            fontsize=8.6, fontweight="bold", color=ROUGE)
    lignes(ax, 980, 727, GARDE_FOUS, 36, 7.4)

    ax.text(45, 880, "Output folders (one status per source file)", ha="left", va="center", fontsize=8.6,
            fontweight="bold", color=TEXTE)
    for x0, x1, c, lg in SORTIES:
        boite(ax, x0, 935, x1, 1150, c)
        n = len(lg)
        lignes(ax, (x0 + x1) / 2, 1048 - (n - 1) * 17, lg, 34, 7.4)
    fig.savefig(sortie, dpi=DPI, facecolor="white")
    plt.close(fig)
    return sortie


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--sortie", default="figure1_pipeline.png")
    print(main(ap.parse_args().sortie))
