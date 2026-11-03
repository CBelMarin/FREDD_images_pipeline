# -*- coding: utf-8 -*-
"""
analyse_contenu.py
==================

Décrit le CONTENU de chaque fichier image, sans décoder les pixels :
modalité (OCT, autofluorescence, fond d'œil, IR, FA…), œil (OD/OG),
orientation des coupes OCT (horizontale / verticale / oblique / radiale),
nombre de B-scans, appareil.

  - .e2e (Heidelberg) : parcours des blocs internes du fichier avec les
    structures de la bibliothèque oct_converter. Un même E2E peut contenir
    plusieurs séries (ex. un OCT + une autofluorescence) : une ligne par série.
    Le bloc « identité patient » (type 9 : nom, date de naissance) n'est
    JAMAIS lu.
  - DICOM : lecture de l'en-tête seulement (pydicom, sans les pixels).
  - images standards (jpg/png…) : seul l'œil peut être déduit du nom
    (jeton OD / OG / OS).

ATTENTION — limites connues (à valider sur de vrais fichiers FREDD avec
diagnostic_e2e.py) :
  - Le format E2E n'est pas documenté publiquement ; les structures utilisées
    sont celles, rétro-ingéniérées, d'oct_converter (et de LibE2E / uocte).
  - Le libellé exact de l'autofluorescence dans le bloc « modalité en face »
    (type 9007) n'est pas documenté : la règle AF_MOTIF ci-dessous est large
    et le libellé brut est toujours recopié dans la colonne `detail`.
  - L'orientation est déduite des coordonnées de début/fin de chaque B-scan
    (bloc 10004, posX1/posY1 -> posX2/posY2) : ligne horizontale si l'angle
    est < 20°, verticale si > 70°.
"""

import math
import re
from collections import Counter, defaultdict

AF_MOTIF = re.compile(r"autofluo|\bfaf\b|\bbaf\b|\baf\b|blue\s*peak|bluepeak|\bnir-?af\b", re.I)
IR_MOTIF = re.compile(r"infra|\bir\b", re.I)
FA_MOTIF = re.compile(r"fluor?escein|\bfa\b", re.I)
ICGA_MOTIF = re.compile(r"\bicga?\b|indocyanin", re.I)
MODALITES_ANGIO = ("angiographie_fluo", "icga")  # hors périmètre FREDD
MC_MOTIF = re.compile(r"multicolor|multi-color|\bmc\b|red.?free|\brf\b", re.I)

SOP_OCT = {"1.2.840.10008.5.1.4.1.1.77.1.5.4",  # Ophthalmic Tomography
           "1.2.840.10008.5.1.4.1.1.77.1.5.7",  # Ophthalmic Optical Coherence Tomography B-scan Volume Analysis
           "1.2.840.10008.5.1.4.1.1.77.1.5.8"}
SOP_FUNDUS = {"1.2.840.10008.5.1.4.1.1.77.1.5.1",  # Ophthalmic Photography 8 bit
              "1.2.840.10008.5.1.4.1.1.77.1.5.2",  # 16 bit
              "1.2.840.10008.5.1.4.1.1.77.1.5.5",  # Wide Field Ophthalmic Photography Stereographic
              "1.2.840.10008.5.1.4.1.1.77.1.5.6"}  # Wide Field Ophthalmic Photography 3D

_OEIL_TOKENS = {"od": "OD", "og": "OG", "os": "OG", "re": "OD", "le": "OG"}


def oeil_depuis_nom(chemin):
    """'..._OD_...', '... OG.jpg' -> 'OD' / 'OG' ; '' si absent ou contradictoire."""
    jetons = set(re.split(r"[^A-Za-z]+", chemin.replace("\\", "/").split("/")[-1].split(" :: ")[-1]))
    trouves = {_OEIL_TOKENS[j.lower()] for j in jetons if j.lower() in ("od", "og", "os")}
    return trouves.pop() if len(trouves) == 1 else ""


def _oeil(v):
    v = (str(v or "")).strip().upper()
    return {"R": "OD", "OD": "OD", "RIGHT": "OD", "L": "OG", "OS": "OG", "OG": "OG", "LEFT": "OG"}.get(v, "")


def orientation_bscans(segments):
    """segments : liste (x1, y1, x2, y2). Retourne 'horizontale', 'verticale',
    'oblique', 'radiale/mixte' ou ''."""
    classes = Counter()
    for x1, y1, x2, y2 in segments:
        dx, dy = abs(x2 - x1), abs(y2 - y1)
        if dx < 1e-6 and dy < 1e-6:
            continue
        a = math.degrees(math.atan2(dy, dx))
        classes["horizontale" if a < 20 else "verticale" if a > 70 else "oblique"] += 1
    if not classes:
        return ""
    if len(classes) == 1:
        return next(iter(classes))
    return "radiale/mixte"


def ligne(modalite="inconnu", lateralite="", orientation="", nb_bscans="", motif_scan="",
          appareil="", detail="", serie="", erreur="", alerte="", source_oeil="",
          sop_uid="", montage=False, sources_montage=()):
    """source_oeil : 'metadonnees' ou 'nom_fichier' (traçabilité de l'œil retenu).
    alerte : incohérence détectée (ex. œil du nom ≠ œil des métadonnées).
    sop_uid, montage, sources_montage : DICOM seulement, pour repérer un montage reconstruit
    (voir `montage_dicom`) et retrouver ses images sources ; absents des CSV."""
    return {"serie": serie, "modalite": modalite, "lateralite": lateralite, "orientation": orientation,
            "nb_bscans": nb_bscans, "motif_scan": motif_scan, "appareil": appareil, "detail": detail,
            "erreur": erreur, "alerte": alerte, "source_oeil": source_oeil,
            "sop_uid": sop_uid, "montage": montage, "sources_montage": list(sources_montage)}


def analyser_e2e(path):
    """Liste de dicts (une par série) décrivant le contenu d'un .e2e."""
    try:
        from oct_converter.readers import E2E
        from oct_converter.readers.binary_structs import e2e_binary as b
    except ImportError:
        return [ligne(erreur="oct_converter absent : contenu E2E non analysé")]
    try:
        e2e = E2E(str(path))
    except Exception as e:  # noqa: BLE001
        return [ligne(erreur=f"E2E illisible ({type(e).__name__})")]

    series = defaultdict(lambda: {"motif": "", "enface": "", "oct_mod": "", "lat": Counter(),
                                  "segments": [], "num_images": 0, "n_fundus": 0, "n_oct": 0,
                                  "appareil": ""})
    TAILLE_LAT = b.lat_structure.sizeof()
    TAILLE_BSCAN = b.bscan_metadata.sizeof()
    TAILLE_PRE = b.pre_data.sizeof()
    import os
    taille_fichier = os.path.getsize(path)
    hors_fichier = 0  # blocs annoncés par le répertoire mais situés (en partie) après la fin du fichier
    try:
        with open(path, "rb") as f:
            pile = []
            for position in e2e.directory_stack:
                f.seek(position + e2e.byte_skip)
                d = b.main_directory_structure.parse(f.read(52))
                for _ in range(d.num_entries):
                    s = b.sub_directory_structure.parse(f.read(44))
                    if s.start > s.pos:
                        pile.append((s.start, s.size))
            for start, taille_bloc in pile:
                if start + e2e.byte_skip + 60 + taille_bloc > taille_fichier:
                    hors_fichier += 1
                f.seek(start + e2e.byte_skip)
                try:
                    c = b.chunk_structure.parse(f.read(60))
                except Exception:  # noqa: BLE001
                    continue
                if c.type == 9:  # identité patient : volontairement ignorée
                    continue
                cle = f"{c.study_id}_{c.series_id}"
                se = series[cle]
                try:
                    if c.type in (9006, 9007, 9008, 9001):
                        txt = [t.strip() for t in b.scan_pattern.parse(f.read(c.size)).text]
                        if c.type == 9006 and not se["motif"]:
                            se["motif"] = txt[0] if txt else ""
                        elif c.type == 9007 and not se["enface"]:
                            se["enface"] = " / ".join(t for t in txt if t)
                        elif c.type == 9008 and not se["oct_mod"]:
                            se["oct_mod"] = " / ".join(t for t in txt if t)
                        elif c.type == 9001 and not se["appareil"]:
                            se["appareil"] = txt[0] if txt else ""
                    elif c.type == 11:
                        se["lat"][_oeil(b.lat_structure.parse(f.read(TAILLE_LAT)).laterality)] += 1
                    elif c.type == 7:
                        se["lat"][_oeil(f.read(1).decode("ascii", "ignore"))] += 1
                    elif c.type == 3:
                        se["lat"][_oeil(b.pre_data.parse(f.read(TAILLE_PRE)).laterality)] += 1
                    elif c.type == 10004:
                        m = b.bscan_metadata.parse(f.read(TAILLE_BSCAN))
                        x1, y1, x2, y2 = m.posX1, m.posY1, m.posX2, m.posY2
                        degenere = abs(x1 - x2) < 1e-6 and abs(y1 - y2) < 1e-6
                        centre_incoherent = abs(m.centrePosX - x1) > 1e-6 or abs(m.centrePosY - y1) > 1e-6
                        if degenere and centre_incoherent:
                            # Bug d'export connu sur certains B-scans isolés (hors volume,
                            # numImages=0) : posX2/posY2 sont écrits identiques à posX1/posY1
                            # (segment dégénéré), alors que `centrePosX/Y` reste correct. Sur les
                            # fichiers non dégénérés (trois fichiers E2E réels de référence), centrePos ==
                            # milieu exact de (posX1,Y1)-(posX2,Y2) à chaque fois ; géométriquement,
                            # un vrai point unique (ex. scan circulaire) aurait donc centrePos ==
                            # ce point -- si centrePos s'en écarte, c'est que posX2/Y2 sont faux,
                            # pas que le segment est réellement ponctuel. On reconstruit alors le
                            # 2e point par symétrie : pos2' = 2*centre - pos1 (validé sur
                            # deux fichiers E2E réels : angle reconstruit = 0.0000°,
                            # dx = 12.000 mm pile, cohérent avec l'image B-scan horizontale
                            # observée visuellement).
                            x2 = 2 * m.centrePosX - x1
                            y2 = 2 * m.centrePosY - y1
                        se["segments"].append((x1, y1, x2, y2))
                        se["num_images"] = max(se["num_images"], int(m.numImages or 0))
                    elif c.type == 1073741824:
                        if c.ind == 0:
                            se["n_fundus"] += 1
                        elif c.ind == 1:
                            # Un bloc ind 1 à 8 bits (~1 octet par pixel : IR de repérage, 512x512,
                            # 768x768...) n'est PAS un B-scan (16 bits, ~2 octets par pixel) : il compte comme
                            # image en face, sinon une série n'ayant que ces images passerait pour de l'OCT.
                            im = b.image_structure.parse(f.read(20))
                            surface = im.width * im.height
                            if surface and c.size / surface < 1.5:
                                se["n_fundus"] += 1
                            else:
                                se["n_oct"] += 1
                except Exception:  # noqa: BLE001
                    continue
    except Exception as e:  # noqa: BLE001
        return [ligne(erreur=f"Lecture E2E interrompue ({type(e).__name__}: {e})")]

    lignes = []
    for cle, se in sorted(series.items()):
        if not (se["motif"] or se["enface"] or se["oct_mod"] or se["segments"] or se["n_fundus"] or se["n_oct"]):
            continue  # série purement technique (pas d'image)
        lat = se["lat"].copy()
        lat.pop("", None)
        oeil = lat.most_common(1)[0][0] if len(lat) == 1 else ("" if not lat else "contradictoire")
        est_oct = bool(se["n_oct"] or se["segments"] or se["oct_mod"] or "oct" in se["motif"].lower()
                       or "volume" in se["motif"].lower())
        enface = se["enface"]
        if est_oct:
            mod = "oct_bscan"
        elif AF_MOTIF.search(enface) or AF_MOTIF.search(se["motif"]):
            mod = "autofluorescence"
        elif ICGA_MOTIF.search(enface):
            mod = "icga"
        elif FA_MOTIF.search(enface):
            mod = "angiographie_fluo"
        elif MC_MOTIF.search(enface):
            mod = "fundus_multicolor"
        elif IR_MOTIF.search(enface):
            mod = "infrarouge"
        elif se["n_fundus"]:
            mod = "fundus_autre"
        else:
            mod = "inconnu"
        nb = se["num_images"] or (se["n_oct"] or "")
        orient = orientation_bscans(se["segments"]) if est_oct else ""
        if est_oct and orient and nb and int(nb) > 1 and orient in ("horizontale", "verticale"):
            orient = f"volume, lignes {orient}s"
        detail = "; ".join(x for x in (
            f"motif={se['motif']}" if se["motif"] else "",
            f"en_face={enface}" if enface else "",
            f"images_fond={se['n_fundus']}" if se["n_fundus"] else "",
            "dont image IR de repérage" if est_oct and se["n_fundus"] else "",
        ) if x)
        lignes.append(ligne(mod, oeil, orient, nb, se["motif"], se["appareil"], detail, serie=cle))
    if not lignes:
        return [ligne(erreur="Aucune série d'image reconnue dans le fichier"
                             + (f" (fichier tronqué : {hors_fichier} bloc(s) hors fichier)" if hors_fichier else ""))]
    if hors_fichier:
        for l in lignes:
            l["alerte"] = (f"fichier E2E tronqué : {hors_fichier} bloc(s) annoncé(s) au-delà de la fin du fichier "
                           f"(copie / transfert incomplet ?)")
    return lignes


SOP_RAW_DATA = "1.2.840.10008.5.1.4.1.1.66"  # Raw Data Storage
SOP_DICOMDIR = "1.2.840.10008.1.3.10"        # Media Storage Directory (fichier DICOMDIR : index, aucune image)


CODE_SOURCE_MONTAGE = "121329"  # DCM « Source image for montage »


def montage_dicom(ds):
    """(est_un_montage, [SOPInstanceUID des images sources]) pour un DICOM d'images.

    Marqueur établi sur UN examen Zeiss CLARUS 500 réel : le montage
    ultra-grand-champ porte `MONTAGE` dans ImageType (« DERIVED, PRIMARY, MONTAGE, COLOR, UWF ») et
    cite ≥ 2 images sources codées 121329 dans SourceImageSequence. Le mot « UWF » seul ne suffit PAS
    (les captures simples du même examen sont aussi étiquetées UWF). À confirmer sur d'autres examens."""
    sources = []
    for it in getattr(ds, "SourceImageSequence", None) or []:
        codes = {str(getattr(c, "CodeValue", "")) for c in (getattr(it, "PurposeOfReferenceCodeSequence", None) or [])}
        uid = str(getattr(it, "ReferencedSOPInstanceUID", "") or "")
        if CODE_SOURCE_MONTAGE in codes and uid:
            sources.append(uid)
    tokens = {str(t).strip().upper() for t in (getattr(ds, "ImageType", None) or [])}
    return ("MONTAGE" in tokens or len(sources) >= 2), sources


def analyser_dicom(path):
    try:
        import pydicom
        ds = pydicom.dcmread(str(path), stop_before_pixels=True, force=True)
    except Exception as e:  # noqa: BLE001
        return [ligne(erreur=f"En-tête DICOM illisible ({type(e).__name__})")]
    sop = str(getattr(ds, "SOPClassUID", "") or "")
    modality = str(getattr(ds, "Modality", "") or "").upper()
    af_presumee = False
    texte = " ".join(str(getattr(ds, k, "") or "") for k in
                     ("ImageType", "SeriesDescription", "AcquisitionDeviceTypeCodeSequence", "ProtocolName"))
    media = str(getattr(getattr(ds, "file_meta", None), "MediaStorageSOPClassUID", "") or "")
    if SOP_DICOMDIR in (sop, media):
        # Un DICOMDIR est l'index d'un support (CD/DVD), pas une image : statut dédié, jamais envoyé à raretia.
        mod = "dicomdir"
    elif sop == SOP_RAW_DATA:
        # En premier : les « Raw Data » Zeiss portent souvent Modality=OP et
        # passeraient sinon pour des fonds d'œil. Ils ne contiennent aucune image affichable.
        mod = "raw_data"
    elif AF_MOTIF.search(texte):
        mod = "autofluorescence"
    elif ICGA_MOTIF.search(texte):
        mod = "icga"
    elif FA_MOTIF.search(texte):
        mod = "angiographie_fluo"  # sans cela, une angiographie (Modality OP) passerait pour un fond d'œil
    elif sop in SOP_OCT or modality in ("OPT", "OCT"):
        mod = "oct_bscan"
    elif (sop in SOP_FUNDUS or modality == "OP") and int(getattr(ds, "SamplesPerPixel", 3) or 3) == 1:
        # Un fond d'œil FREDD est en COULEUR : une image DICOM d'ophtalmologie en niveaux de gris, sans
        # mention d'angiographie, est comptée comme autofluorescence PRÉSUMÉE (hypothèse à vérifier).
        mod = "autofluorescence"
        af_presumee = True
    elif sop in SOP_FUNDUS or modality == "OP":
        mod = "fundus"
    else:
        mod = "inconnu"
    oeil = _oeil(getattr(ds, "ImageLaterality", "") or getattr(ds, "Laterality", ""))
    nb = str(getattr(ds, "NumberOfFrames", "") or "") if mod == "oct_bscan" else ""
    appareil = " ".join(str(getattr(ds, k, "") or "") for k in ("Manufacturer", "ManufacturerModelName")).strip()
    detail = f"Modality={modality}" + (f"; ImageType={'/'.join(ds.ImageType)}" if "ImageType" in ds else "")
    orientation = ""
    alerte = ""
    if mod == "oct_bscan":
        # Pas d'OCT en DICOM attendu dans FREDD ; s'il y en a, orientation non lue => colonne « Autre ».
        # Un DICOM déjà traité par le pipeline porte « orientation : … » dans ImageComments : on la reprend.
        m_or = re.search(r"orientation\s*:\s*([^;]+)", str(getattr(ds, "ImageComments", "") or ""))
        orientation = m_or.group(1).strip() if m_or else "autre (DICOM : orientation non lue)"
    if af_presumee:
        alerte = "AF présumée : image en niveaux de gris sans mention d'autofluorescence"
    montage, sources = montage_dicom(ds) if mod in ("fundus", "autofluorescence") else (False, [])
    if montage:
        detail += f"; montage reconstruit ({len(sources)} image(s) source citée(s))"
    return [ligne(mod, oeil, orientation, nb, "", appareil, detail, alerte=alerte, sop_uid=str(getattr(ds, "SOPInstanceUID", "") or ""),
                  montage=montage, sources_montage=sources)]


def _alerter(r, message):
    r["alerte"] = f"{r['alerte']} | {message}" if r.get("alerte") else message


def analyser(path, categorie, chemin_affiche=""):
    """Point d'entrée : liste de lignes décrivant le contenu du fichier."""
    if categorie == "E2E_HEIDELBERG":
        res = analyser_e2e(path)
    elif categorie == "DICOM":
        res = analyser_dicom(path)
    else:
        res = [ligne("image_standard")]
    nom = oeil_depuis_nom(chemin_affiche or str(path))
    for r in res:
        if r["lateralite"] in ("OD", "OG"):
            r["source_oeil"] = "metadonnees"
            if nom and nom != r["lateralite"]:
                # Les métadonnées priment, mais la contradiction est signalée :
                # erreur de nommage OU métadonnée fausse, à vérifier à l'œil.
                _alerter(r, f"œil du nom de fichier ({nom}) ≠ œil des métadonnées ({r['lateralite']})")
        elif not r["lateralite"] and nom:
            r["lateralite"] = nom
            r["source_oeil"] = "nom_fichier"
            r["detail"] = (r["detail"] + "; " if r["detail"] else "") + "œil déduit du nom de fichier"
        elif r["lateralite"] == "contradictoire":
            _alerter(r, "œil contradictoire entre blocs internes du fichier")
    return res
