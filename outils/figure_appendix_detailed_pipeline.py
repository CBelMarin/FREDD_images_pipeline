# -*- coding: utf-8 -*-
"""
figure_appendix_detailed_pipeline.py : dessine la Figure A1 de l'article (architecture detaillee du pipeline).

Usage :  python figure_appendix_detailed_pipeline.py [--sortie figure_appendix_detailed_pipeline.png] [--version 1.12.5]
Necessite seulement matplotlib. Unites : points typographiques ; la figure fait 9,8 pouces de large (page paysage),
les tailles de police sont donc celles qui apparaissent a l'impression (corps 7 pt, titres 8,5 pt).
"""
import argparse

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch, Polygon

WPT = 706.0                      # largeur de la figure (pt) = 9,8 pouces
GUT = 16.0                       # gouttiere entre colonnes
COLW = (WPT - 2 * 6 - GUT) / 2   # largeur d'une colonne
X0 = 6.0
BLEU, VERT, ORANGE, ROUGE, GRIS = "#2563eb", "#0f766e", "#b45309", "#b91c1c", "#6b7280"
TEXTE = "#1f2937"
FONT = "Liberation Sans"
CORPS, TITRE, SOUS = 7.0, 8.6, 7.0
PAS = 8.7                        # interligne (pt)
PAD = 6.0


class Fig:
    def __init__(self):
        plt.rcParams["font.family"] = [FONT, "DejaVu Sans"]
        self.fig = plt.figure(figsize=(WPT / 72, 8), dpi=72)
        self.ax = self.fig.add_axes([0, 0, 1, 1])
        self.rend = self.fig.canvas.get_renderer()

    def larg(self, s, taille, bold=False):
        t = self.ax.text(0, 0, s, fontsize=taille, fontweight="bold" if bold else "normal")
        w = t.get_window_extent(self.rend).width
        t.remove()
        return w

    def envelopper(self, s, w, taille, bold=False):
        """Coupe s en lignes de largeur <= w ; retrait en suspension apres une puce."""
        puce = s.startswith("•") or s.startswith("   ")
        indent = self.larg("•  ", taille) if puce else 0
        res, cur, lim = [], "", w
        for mot in s.split(" "):
            essai = (cur + " " + mot) if cur else mot
            if self.larg(essai, taille, bold) <= lim or not cur:
                cur = essai
            else:
                res.append(cur)
                cur = mot
                lim = w - indent
        res.append(cur)
        return [(r, 0 if k == 0 else indent) for k, r in enumerate(res)]

    # -- elements -----------------------------------------------------------------------------------------------
    def boite(self, x, y, w, h, bord, fond="white", lw=1.0, style="-", rayon=4):
        self.ax.add_patch(FancyBboxPatch((x, y), w, h, boxstyle=f"round,pad=0,rounding_size={rayon}",
                                         fc=fond, ec=bord, lw=lw, ls=style, zorder=2))

    def txt(self, x, y, s, taille=CORPS, c=TEXTE, bold=False, **kw):
        self.ax.text(x, y, s, ha="left", va="center", fontsize=taille, color=c, zorder=5,
                     fontweight="bold" if bold else "normal", **kw)

    def cadre(self, x, y, w, couleur, titre, lignes, fond="white", lw=1.0, style="-", t_taille=TITRE):
        """Cadre titre + corps ; lignes = str ou ('sub', str) ; renvoie la hauteur."""
        inner = w - 2 * PAD - 2
        corps = []
        for l in lignes:
            sub = isinstance(l, tuple)
            s = l[1] if sub else l
            for r, ind in self.envelopper(s, inner, SOUS if sub else CORPS, sub):
                corps.append((r, ind, sub))
        h = PAD + 11 + len(corps) * PAS + PAD - 1
        self.boite(x, y, w, h, couleur, fond, lw, style)
        self.txt(x + PAD + 2, y + PAD + 3, titre, t_taille, couleur, True)
        yy = y + PAD + 11 + PAS / 2 + 1
        for r, ind, sub in corps:
            self.txt(x + PAD + 2 + ind, yy, r, SOUS if sub else CORPS, TEXTE, sub)
            yy += PAS
        return h

    def module(self, x, y, w, etapes):
        """Cadre 4b : titre + une sous-boite par etape (titre a gauche, texte a droite)."""
        tw = 66.0
        txt_w = w - 2 * PAD - 4 - tw - 14
        blocs, htot = [], PAD + 11 + 3
        for t, texte in etapes:
            lg = self.envelopper(texte, txt_w, CORPS - 0.2)
            hb = max(len(lg) * PAS + 5, 18)
            blocs.append((t, lg, hb))
            htot += hb + 4
        htot += PAD - 3
        self.boite(x, y, w, htot, VERT, "#f0fdfa", 1.8)
        self.txt(x + PAD + 2, y + PAD + 3, "4b  Image processing module (pipeline_wp2, frozen), per image",
                 TITRE, VERT, True)
        yy = y + PAD + 11 + 3
        for t, lg, hb in blocs:
            self.boite(x + PAD, yy, w - 2 * PAD, hb, VERT, "white", 0.8, rayon=3)
            self.txt(x + PAD + 4, yy + hb / 2, t, SOUS + 0.3, VERT, True)
            y2 = yy + hb / 2 - (len(lg) - 1) * PAS / 2
            for r, _ in lg:
                self.txt(x + PAD + 4 + tw + 6, y2, r, CORPS - 0.2)
                y2 += PAS
            yy += hb + 4
        return htot

    def fleche(self, pts, c=GRIS, lw=1.0, tete=6):
        xs, ys = zip(*pts)
        self.ax.plot(xs, ys, color=c, lw=lw, zorder=1, solid_capstyle="butt")
        self.ax.annotate("", xy=pts[-1], xytext=pts[-2], zorder=6,
                         arrowprops=dict(arrowstyle="-|>", color=c, lw=0, mutation_scale=tete, shrinkA=0, shrinkB=0))


GAUCHE = [
    (GRIS, "Inputs",
     ["• Transfer storage: nested zip archives, DICOM (with or without extension), E2E, JPEG",
      "• FREDD data (list of pseudonyms, center); eCRF export (declared images)"]),
    (BLEU, "1  Inventory",
     ["• recursive walk; archives read in memory; SHA-256 of each file",
      "• format: DICOM / E2E / standard image / other; unreadable files reported"]),
    (BLEU, "2  Content analysis (headers only, pixels not decoded)",
     [("sub", "DICOM"),
      "• SOP class DICOMDIR or Raw Data → no image",
      "• AF / ICGA / FA text (ImageType, SeriesDescription, device, ProtocolName) → AF or angiography",
      "• SOP class OCT or Modality OPT → OCT; 3-channel ophthalmic photograph → color fundus",
      "• 1-channel ophthalmic photograph with no mention → presumed AF (alert)",
      "• ImageType MONTAGE or ≥ 2 sources with code 121329 → UWF montage",
      ("sub", "E2E (series blocks)"),
      "• B-scans / section coordinates → OCT; en face image AF, ICGA, FA, multicolor, infrared; square 8-bit blocks of the B-scan index → en face (not OCT)",
      "• eye; orientation: section angle (< 20° horizontal, > 70° vertical, mixed = radial, volume)",
      ("sub", "Other formats (JPEG...)"),
      "• no usable metadata; device: manufacturer and model"]),
    (BLEU, "3  Sorting and decisions (one status per source file)",
     ["• pseudonym read from the path (first 8 characters); unknown, undetected or ambiguous → set aside",
      "• match to FREDD data; center (alias table)",
      "• deduplication on (SHA-256, patient): copies → ignored as duplicates",
      "• Raw Data, DICOMDIR → no image; infrared alone, angiography → excluded; other formats → unsupported",
      "• montage: excluded only if all native sources are present for the patient; otherwise kept with an alert; natives flagged as \"montage component\"",
      "• resumption: (SHA-256, patient) already processed → kept as is (full reprocessing only if forced)",
      "• files to process grouped by center, in packets of 200"]),
]
ETAPES_4B = [
    ("1 Reading", "DICOM / E2E; angiographies ignored; implausible dates not copied; field: 4 black corners = wide field (55°), otherwise macular (30°); montage from metadata"),
    ("2 Cropping", "black borders removed (fundus, AF); black and white borders (OCT)"),
    ("3 Quality", "indicative score (sharpness, contrast, brightness, uniformity); usable area estimated; \"limited quality\" flag only if the usable area is severely obstructed (image still produced)"),
    ("4 Standardization", "type-specific denoising; CLAHE (clip limit 2, 8×8 grid); resizing to 2048×2048 (fundus, AF) or 1024×496 (OCT); RGB"),
    ("5 Detection", "optic disc and fovea (not OCT); laterality (metadata, otherwise disc position); angle of view only if measured"),
    ("6 Writing", "new DICOM created from scratch: only whitelisted metadata are written (patient, remapped study, series, instance, image, eye, device, comments, annotations)"),
]
CORPS_4D = ["• no name or identifier in files, tags, pixels; forbidden DICOM tags empty; study UID remapped",
            "• embedded-annotation indicator; pixel text detector (no OCR: identification banners)",
            "• failure → quarantine, never transmitted; otherwise PatientID = FREDD id",
            "• technical quality written in ImageComments; filing by center, date, patient",
            "• resumption state written after each packet"]
CORPS_5A = ["by patient, modality, eye, OCT type:",
            "• OK / not found / found but not declared / mismatched ID",
            "• OCT horizontal, vertical, other (a valid volume counts as \"other\")",
            "• device declared vs found; AF and OCT in the same file",
            "• UWF counted as non-UWF for the reconciliation"]
CORPS_5B = ["• image_log (status per source file); output_state (one line per image produced)",
            "• per-center summary; run summary; patient_reconciliation; action list",
            "• catalog (field, UWF mode, quality); execution.json (versions, code fingerprints)"]


def main(sortie="figure_appendix_detailed_pipeline.png", version=None):
    F = Fig()
    ax = F.ax
    xl, xr = X0, X0 + COLW + GUT
    titre_fig = "FREDD image pipeline" + (f" v{version}" if version else "") + ": detailed architecture"
    F.txt(X0 + 1, 9, titre_fig, 10.5, TEXTE, True)
    top = 22.0
    G = 6.0   # espace vertical entre cadres

    # colonne de gauche
    yl, centres_g = top, []
    for c, t, lg in GAUCHE:
        h = F.cadre(xl, yl, COLW, c, t, lg, lw=1.2)
        centres_g.append((yl, h))
        yl += h + G
    y5a = yl
    h5a = F.cadre(xl, y5a, COLW, ORANGE, "5a  eCRF reconciliation", CORPS_5A, lw=1.2)
    y5b = y5a + h5a + G
    h5b = F.cadre(xl, y5b, COLW, ORANGE, "5b  Reports", CORPS_5B, lw=1.2)
    bas_g = y5b + h5b

    # colonne de droite
    yr = top
    y4a = yr
    h4a = F.cadre(xr, y4a, COLW, VERT, "4a  Packet staging",
                  ["copy under a neutral name <fredd_id>__<hash>; source identifiers collected (tags, declared embedded text)"], lw=1.2)
    yr += h4a + G
    y4b = yr
    h4b = F.module(xr, y4b, COLW, ETAPES_4B)
    yr += h4b + G
    y4c = yr
    h4c = F.cadre(xr, y4c, COLW, VERT, "4c  Image-by-image retry",
                  ["if the packet fails or a file yields no output (except Raw Data, DICOMDIR, infrared-only E2E)"], lw=1.2)
    yr += h4c + G
    y4d = yr
    h4d = F.cadre(xr, y4d, COLW, ROUGE, "4d  Post-processing checks (independent safeguards)", CORPS_4D, lw=1.2)
    yr += h4d + G
    yo = yr
    ho = F.cadre(xr, yo, COLW, GRIS, "Outputs (folders)",
                 ["processed_images/<center>/<date>/<id>; quarantine; no_patient/; unsupported_formats/; processing_failures/"], lw=1.2)
    yr += ho + G
    yx = yr
    hx = F.cadre(xr, yx, COLW, ORANGE, "Outside the pipeline",
                 ["multimodal registration prototype (not validated); ZIP export"], fond="#fefce8", lw=1.2, style=(0, (4, 2)), t_taille=8.0)
    bas_d = yx + hx

    # fleches
    cx_l, cx_r = xl + COLW / 2, xr + COLW / 2
    for (ya, ha), (yb, _) in zip(centres_g[:-1], centres_g[1:]):
        F.fleche([(cx_l, ya + ha), (cx_l, yb)])
    ya, ha = centres_g[-1]
    F.fleche([(cx_l, ya + ha), (cx_l, y5a)])
    ax.text(cx_l + 6, ya + ha + G / 2, "statuses", fontsize=6.5, style="italic", color=GRIS, ha="left", va="center", zorder=5)
    F.fleche([(cx_l, y5a + h5a), (cx_l, y5b)])
    gx = xl + COLW + GUT / 2
    F.fleche([(xl + COLW, ya + ha / 2), (gx, ya + ha / 2), (gx, y4a + h4a / 2), (xr, y4a + h4a / 2)])
    ax.text(gx, (ya + ha / 2 + y4a + h4a / 2) / 2, "packets", rotation=90, ha="center", va="center", fontsize=6.5,
            style="italic", color=GRIS, bbox=dict(fc="white", ec="none", pad=1), zorder=5)
    F.fleche([(cx_r, y4a + h4a), (cx_r, y4b)], VERT)
    F.fleche([(cx_r, y4b + h4b), (cx_r, y4c)], VERT)
    F.fleche([(cx_r, y4c + h4c), (cx_r, y4d)], VERT)
    F.fleche([(cx_r, y4d + h4d), (cx_r, yo)], ROUGE)
    # 4d -> 5a / 5b (par la gouttiere)
    y_t = y4d + h4d / 2
    gx2 = xl + COLW + GUT / 2 + 3
    F.fleche([(xr, y_t), (gx2, y_t), (gx2, y5b + h5b / 2), (xl + COLW, y5b + h5b / 2)], ROUGE)

    H = max(bas_g, bas_d) + 6
    F.fig.set_size_inches(WPT / 72, H / 72)
    ax.set_xlim(0, WPT)
    ax.set_ylim(H, 0)
    ax.axis("off")
    F.fig.savefig(sortie, dpi=300, facecolor="white")
    plt.close(F.fig)
    return sortie, WPT / 72, H / 72


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--sortie", default="figure_appendix_detailed_pipeline.png")
    ap.add_argument("--version", default=None, help="ajoute le numero de version au titre (ex. 1.12.5)")
    a = ap.parse_args()
    print(main(a.sortie, a.version))
