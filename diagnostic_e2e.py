#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
diagnostic_e2e.py
=================

Outil de VALIDATION de l'analyse des fichiers Heidelberg .e2e sur de vrais
fichiers FREDD, à lancer sur quelques fichiers dont on connaît le contenu
(ex. un E2E « OCT ligne horizontale OD », un « OCT + AF », un « OCT
vertical »).

Pour chaque fichier, il affiche et écrit dans un CSV :
  1. l'interprétation retenue par le pipeline (modalité, œil, orientation,
     nombre de B-scans) ;
  2. les éléments BRUTS sur lesquels elle repose, série par série : types
     de blocs présents, octets de latéralité, libellés des blocs 9001
     (appareil), 9005 (structure), 9006 (motif), 9007 (modalité en face),
     9008 (modalité OCT), codes de type des images, coordonnées des
     premières / dernières coupes.

AUCUNE donnée identifiante n'est lue : les blocs 9 (identité patient),
13 (application), 17 (diagnostic), 9000 (nom d'étude) et les UID (52, 54,
1000, 1001) sont ignorés. Le résultat peut donc être transmis pour
analyse — à vérifier quand même avant envoi, les libellés saisis dans
HEYEX pouvant exceptionnellement contenir du texte libre.

UTILISATION
  python diagnostic_e2e.py "D:\\exemples\\a.e2e" "D:\\exemples\\b.e2e"
  python diagnostic_e2e.py "D:\\exemples" --sortie diagnostic_e2e.csv
"""

import argparse
import csv
import sys
from collections import Counter, defaultdict
from pathlib import Path

import analyse_contenu as ac

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

TYPES_IGNORES = {9, 13, 17, 9000, 52, 54, 1000, 1001}  # identité, texte libre, UID
TYPES_TEXTE = {9001: "appareil", 9005: "structure", 9006: "motif", 9007: "en_face", 9008: "oct_modalite"}
TYPES_LAT = {11: 14, 59: 14, 3: 4, 5: 2}  # type -> position de l'octet de latéralité (eyepy / oct_converter)


def brut_e2e(path):
    """Éléments bruts par série (clé 'étude_série')."""
    from oct_converter.readers import E2E
    from oct_converter.readers.binary_structs import e2e_binary as b

    e2e = E2E(str(path))
    series = defaultdict(lambda: {"types": Counter(), "lat": Counter(), "textes": {}, "img": Counter(),
                                  "segments": []})
    with open(path, "rb") as f:
        pile = []
        for position in e2e.directory_stack:
            f.seek(position + e2e.byte_skip)
            d = b.main_directory_structure.parse(f.read(52))
            for _ in range(d.num_entries):
                s = b.sub_directory_structure.parse(f.read(44))
                if s.start > s.pos:
                    pile.append(s.start)
        for start in pile:
            f.seek(start + e2e.byte_skip)
            try:
                c = b.chunk_structure.parse(f.read(60))
            except Exception:  # noqa: BLE001
                continue
            if c.type in TYPES_IGNORES:
                continue
            se = series[f"{c.study_id}_{c.series_id}"]
            se["types"][c.type] += 1
            try:
                if c.type in TYPES_LAT:
                    data = f.read(TYPES_LAT[c.type] + 1)
                    octet = data[TYPES_LAT[c.type]] if len(data) > TYPES_LAT[c.type] else None
                    se["lat"][f"t{c.type}:{chr(octet) if octet in (76, 82) else octet}"] += 1
                elif c.type in TYPES_TEXTE:
                    txt = [t.strip() for t in b.scan_pattern.parse(f.read(c.size)).text if t.strip()]
                    se["textes"].setdefault(TYPES_TEXTE[c.type], " / ".join(txt))
                elif c.type == 10004:
                    m = b.bscan_metadata.parse(f.read(b.bscan_metadata.sizeof()))
                    se["segments"].append((round(m.posX1, 2), round(m.posY1, 2), round(m.posX2, 2), round(m.posY2, 2)))
                elif c.type == 1073741824:
                    im = b.image_structure.parse(f.read(20))
                    se["img"][f"ind{c.ind}:type{im.type}:{im.width}x{im.height}"] += 1
            except Exception as e:  # noqa: BLE001
                se["textes"].setdefault("erreur", f"{type(e).__name__} sur bloc {c.type}")
    return series


def main():
    p = argparse.ArgumentParser(description="Diagnostic du contenu de fichiers .e2e (sans données identifiantes).")
    p.add_argument("chemins", nargs="+", help="Fichiers .e2e ou dossiers (parcourus récursivement).")
    p.add_argument("--sortie", default="diagnostic_e2e.csv")
    a = p.parse_args()

    fichiers = []
    for c in map(Path, a.chemins):
        fichiers += sorted(c.rglob("*.[eE]2[eE]")) if c.is_dir() else [c]
    lignes = []
    for fp in fichiers:
        print(f"\n=== {fp.name} ===")
        interp = {r["serie"]: r for r in ac.analyser(fp, "E2E_HEIDELBERG", str(fp))}
        try:
            brut = brut_e2e(fp)
        except Exception as e:  # noqa: BLE001
            print(f"  illisible : {type(e).__name__}: {e}")
            lignes.append({"fichier": fp.name, "erreur": f"{type(e).__name__}: {e}"})
            continue
        for cle in sorted(brut):
            se, r = brut[cle], interp.get(cle, {})
            if not (se["img"] or se["segments"] or se["textes"]):
                continue
            seg = se["segments"]
            ligne = {
                "fichier": fp.name, "serie": cle,
                "INTERPRETATION": " ".join(str(x) for x in (r.get("modalite", "-"), r.get("lateralite", ""),
                                                             r.get("orientation", ""),
                                                             f"x{r['nb_bscans']}" if r.get("nb_bscans") else "") if x),
                "lateralite_brute": ", ".join(f"{k}(x{v})" for k, v in se["lat"].items()),
                **{k: se["textes"].get(k, "") for k in list(TYPES_TEXTE.values()) + ["erreur"]},
                "images": ", ".join(f"{k}(x{v})" for k, v in se["img"].items()),
                "nb_coupes_10004": len(seg),
                "premiere_coupe_x1y1x2y2": seg[0] if seg else "",
                "derniere_coupe_x1y1x2y2": seg[-1] if seg else "",
                "types_blocs": ", ".join(f"{t}(x{n})" for t, n in sorted(se["types"].items())),
            }
            lignes.append(ligne)
            print(f"  série {cle:<12} -> {ligne['INTERPRETATION']}")
            for k in ("lateralite_brute", "motif", "en_face", "oct_modalite", "appareil", "images",
                      "premiere_coupe_x1y1x2y2", "derniere_coupe_x1y1x2y2"):
                if ligne.get(k):
                    print(f"      {k:<26} {ligne[k]}")
    if lignes:
        cles = list(dict.fromkeys(k for l in lignes for k in l))
        with open(a.sortie, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=cles, delimiter=";")
            w.writeheader()
            w.writerows(lignes)
        print(f"\nRésultat : {Path(a.sortie).resolve()}")


if __name__ == "__main__":
    main()
