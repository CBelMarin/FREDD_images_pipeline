# -*- coding: utf-8 -*-
"""
visionneuse.py — Visionneuse locale DICOM / E2E (FREDD)
=======================================================

Glisser un fichier .dcm / .e2e dans la page pour :
  - voir chaque image qu'il contient (fonds d'œil, images en face, B-scans OCT
    avec un curseur), avec la modalité, l'œil et l'orientation détectés ;
  - vérifier s'il reste du texte identifiant : en-tête DICOM (nom, IPP, date de
    naissance, établissement…), bloc identité des E2E, et à l'œil sur l'image
    elle-même (texte incrusté) ;
  - savoir ce que le pipeline en ferait (Raw Data, IR exclue…).

Tout reste sur ce poste : le serveur n'écoute que sur 127.0.0.1, les fichiers ne
sont jamais écrits ailleurs qu'un dossier temporaire supprimé aussitôt la
lecture faite, et les images décodées ne sont gardées qu'en mémoire.

UTILISATION
  python visionneuse.py            (ouvre le navigateur sur http://127.0.0.1:8765)
  python visionneuse.py --port 9000 --pas-de-navigateur

Dépendances : celles du pipeline (pydicom, oct-converter, numpy, Pillow,
décodeurs pylibjpeg pour les DICOM compressés).
"""

import argparse
import io
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import uuid
import webbrowser
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import analyse_contenu as ac  # noqa: E402

__version__ = "1.0.0"

SOP_RAW = "1.2.840.10008.5.1.4.1.1.66"
MAX_CACHE = 6            # fichiers gardés en mémoire (les plus récents)

# Tags DICOM pouvant identifier le patient (ou permettre de remonter à lui).
TAGS_IDENTIFIANTS = [
    ("PatientName", "Nom du patient"),
    ("PatientID", "Identifiant patient (IPP)"),
    ("OtherPatientIDs", "Autres identifiants"),
    ("OtherPatientNames", "Autres noms"),
    ("PatientBirthName", "Nom de naissance"),
    ("PatientMotherBirthName", "Nom de naissance de la mère"),
    ("PatientBirthDate", "Date de naissance"),
    ("PatientAddress", "Adresse"),
    ("PatientTelephoneNumbers", "Téléphone"),
    ("MedicalRecordLocator", "N° de dossier médical"),
    ("AccessionNumber", "N° d'examen (accession)"),
    ("InstitutionName", "Établissement"),
    ("InstitutionAddress", "Adresse de l'établissement"),
    ("ReferringPhysicianName", "Médecin prescripteur"),
    ("PerformingPhysicianName", "Médecin réalisateur"),
    ("OperatorsName", "Opérateur"),
    ("StationName", "Poste d'acquisition"),
    ("DeviceSerialNumber", "N° de série de l'appareil"),
]
TAGS_CONTEXTE = [("PatientSex", "Sexe"), ("PatientAge", "Âge"), ("StudyDate", "Date d'examen"),
                 ("Manufacturer", "Fabricant"), ("ManufacturerModelName", "Modèle"),
                 ("Modality", "Modality"), ("SOPClassUID", "SOP Class")]

LIBELLES_MODALITE = {
    "oct_bscan": "OCT (B-scan)", "fundus": "Fond d'œil", "autofluorescence": "Autofluorescence",
    "infrarouge": "Infrarouge (IR)", "angiographie_fluo": "Angiographie à la fluorescéine",
    "icga": "Angiographie au vert d'indocyanine", "fundus_multicolor": "Multicolor",
    "fundus_autre": "Image en face (type non précisé)", "raw_data": "Raw Data (sans image)",
    "inconnu": "Type inconnu", "image_standard": "Image standard",
}


def vers_uint8(img):
    """Image quelconque -> uint8 affichable (étirement 0,5–99,5 % si besoin)."""
    a = np.asarray(img)
    if a.dtype == np.uint8:
        return a
    a = a.astype(np.float32)
    finis = a[np.isfinite(a)]
    if finis.size == 0:
        return np.zeros(a.shape, dtype=np.uint8)
    bas, haut = np.percentile(finis, [0.5, 99.5])
    if haut <= bas:
        haut = bas + 1
    a = np.nan_to_num(a, nan=bas)
    return (np.clip((a - bas) / (haut - bas), 0, 1) * 255).astype(np.uint8)


def png(img):
    from PIL import Image
    a = vers_uint8(img)
    if a.ndim == 3 and a.shape[2] == 1:
        a = a[:, :, 0]
    buf = io.BytesIO()
    Image.fromarray(a).save(buf, format="PNG", compress_level=3)
    return buf.getvalue()


def _txt(v):
    if v is None:
        return ""
    if isinstance(v, bytes):
        v = v.decode("latin-1", "replace")
    if isinstance(v, (list, tuple)):
        v = " \\ ".join(_txt(x) for x in v)
    return str(v).strip()


def type_fichier(nom, data):
    ext = Path(nom).suffix.lower()
    if ext == ".e2e" or data[:4] == b"CMDb" or data[:21] == b"E2EMultipleVolumeFile":
        return "E2E"
    if ext in (".dcm", ".dicom") or data[128:132] == b"DICM":
        return "DICOM"
    return "DICOM?"  # dernier essai : DICOM sans préambule ni extension


def _ligne_contenu(c):
    return {"modalite": c.get("modalite", ""), "libelle": LIBELLES_MODALITE.get(c.get("modalite", ""), c.get("modalite", "")),
            "oeil": c.get("lateralite", ""), "orientation": c.get("orientation", ""),
            "nb_bscans": c.get("nb_bscans", ""), "detail": c.get("detail", ""),
            "alerte": c.get("alerte", ""), "erreur": c.get("erreur", "")}


def analyser_dicom(chemin, nom):
    import pydicom
    info = {"format": "DICOM", "identifiants": [], "contexte": [], "alertes": [], "pipeline": ""}
    series = []
    try:
        ds = pydicom.dcmread(str(chemin), force=True)
    except Exception as e:  # noqa: BLE001
        info["erreur"] = f"Fichier DICOM illisible : {type(e).__name__}: {e}"
        return info, series
    for kw, lib in TAGS_IDENTIFIANTS:
        if kw in ds:
            info["identifiants"].append({"libelle": lib, "tag": kw, "valeur": _txt(ds[kw].value)})
    burned = _txt(getattr(ds, "BurnedInAnnotation", ""))
    if burned:
        info["identifiants"].append({"libelle": "Texte incrusté déclaré (BurnedInAnnotation)", "tag": "BurnedInAnnotation",
                                     "valeur": burned, "public": True})
        if burned.upper() == "YES":
            info["alertes"].append("Le fichier déclare du texte incrusté dans l'image (BurnedInAnnotation = YES).")
    for kw, lib in TAGS_CONTEXTE:
        v = _txt(getattr(ds, kw, ""))
        if v:
            info["contexte"].append({"libelle": lib, "valeur": v})
    commentaires = _txt(getattr(ds, "ImageComments", ""))
    if commentaires:  # DICOM traité par le pipeline : « B-scan 3/49 ; orientation : horizontale »
        info["contexte"].append({"libelle": "Commentaires de l'image", "valeur": commentaires})
    contenu = [_ligne_contenu(c) for c in ac.analyser(chemin, "DICOM", nom)]
    info["contenu"] = contenu
    sop = _txt(getattr(ds, "SOPClassUID", ""))
    if sop == SOP_RAW:
        info["pipeline"] = ("Raw Data : données propriétaires de l'appareil, sans image. "
                            "Le pipeline ne l'envoie pas au traitement (statut NON_TRAITEE_RAW_DATA).")
    if "PixelData" not in ds:
        info["alertes"].append("Aucune image dans ce fichier (pas de données pixel).")
        return info, series
    try:
        arr = ds.pixel_array
    except Exception as e:  # noqa: BLE001
        info["alertes"].append(f"Pixels non décodables ({type(e).__name__}: {e}). "
                               "Compression non prise en charge ? (décodeurs pylibjpeg installés ?)")
        return info, series
    photo = _txt(getattr(ds, "PhotometricInterpretation", ""))
    n = int(getattr(ds, "NumberOfFrames", 1) or 1)
    rgb = int(getattr(ds, "SamplesPerPixel", 1) or 1) == 3
    if photo.startswith("YBR") and rgb:
        try:
            from pydicom.pixels import convert_color_space
            arr = convert_color_space(arr, photo, "RGB")
        except Exception:  # noqa: BLE001
            pass
    if photo == "MONOCHROME1":
        arr = arr.max() - arr
    images = [arr[i] for i in range(n)] if n > 1 else [arr]
    c0 = contenu[0] if contenu else {}
    m_or = re.search(r"orientation\s*:\s*([^;]+)", commentaires)
    series.append({"titre": c0.get("libelle") or "Image", "oeil": c0.get("oeil", ""),
                   "orientation": c0.get("orientation", "") or (m_or.group(1).strip() if m_or else ""),
                   "exclue_ir": False, "images": images})
    return info, series


def _cle_serie(image_id):
    p = str(image_id or "").rstrip("_").split("_")
    return "_".join(p[1:3]) if len(p) >= 3 else ""


def analyser_e2e(chemin, nom):
    info = {"format": "E2E (Heidelberg)", "identifiants": [], "contexte": [], "alertes": [], "pipeline": ""}
    series = []
    try:
        from oct_converter.readers import E2E
    except ImportError:
        info["erreur"] = "oct-converter n'est pas installé : impossible de lire les E2E."
        return info, series
    contenu = [_ligne_contenu(c) for c in ac.analyser(chemin, "E2E_HEIDELBERG", nom)]
    info["contenu"] = contenu
    par_serie = {c["serie"]: c for c in ac.analyser_e2e(chemin) if not c.get("erreur")}
    try:
        e2e = E2E(str(chemin))
    except Exception as e:  # noqa: BLE001
        info["erreur"] = f"Fichier E2E illisible : {type(e).__name__}: {e}"
        return info, series
    try:
        volumes = e2e.read_oct_volume()
    except Exception as e:  # noqa: BLE001
        volumes = []
        info["alertes"].append(f"Lecture des volumes OCT impossible : {type(e).__name__}: {e}")
    try:
        fonds = e2e.read_fundus_image()
    except Exception as e:  # noqa: BLE001
        fonds = []
        info["alertes"].append(f"Lecture des images en face impossible : {type(e).__name__}: {e}")
    series_oct = {_cle_serie(getattr(v, "volume_id", "")) for v in volumes}

    # Bloc identité (lu par oct_converter pendant la lecture ci-dessus)
    for attr, lib in (("surname", "Nom"), ("first_name", "Prénom"), ("birthdate", "Date de naissance"),
                      ("patient_id", "Identifiant patient"), ("sex", "Sexe")):
        v = _txt(getattr(e2e, attr, ""))
        if v:
            info["identifiants"].append({"libelle": lib, "tag": f"bloc identité E2E : {attr}", "valeur": v})
    if not info["identifiants"]:
        info["contexte"].append({"libelle": "Bloc identité", "valeur": "absent ou vide (fichier anonymisé ?)"})

    for f in fonds:
        cle = _cle_serie(getattr(f, "image_id", ""))
        c = par_serie.get(cle, {})
        mod = c.get("modalite", "")
        enface = next((x.split("=", 1)[1] for x in (c.get("detail") or "").split("; ") if x.startswith("en_face=")), "")
        est_af = mod == "autofluorescence" or bool(ac.AF_MOTIF.search(enface))
        reperage = cle in series_oct and not est_af
        ir = mod == "infrarouge" or reperage
        titre = ("IR de repérage de l'OCT" if reperage else LIBELLES_MODALITE.get(mod, "Image en face"))
        lib_brut = enface.split(" / ")[0].strip()
        if lib_brut and not reperage and lib_brut.lower() not in titre.lower():
            titre += f" — « {lib_brut} »"
        img = getattr(f, "image", None)
        if img is None:
            continue
        oeil = c.get("lateralite", "") or {"R": "OD", "L": "OG"}.get(_txt(getattr(f, "laterality", "")), "")
        series.append({"titre": titre, "oeil": oeil, "orientation": "", "exclue_ir": ir, "images": [np.asarray(img)]})
    for v in volumes:
        cle = _cle_serie(getattr(v, "volume_id", ""))
        c = par_serie.get(cle, {})
        vol = getattr(v, "volume", None)
        if vol is None:
            continue
        images = [np.asarray(b) for b in vol]
        if not images:
            continue
        oeil = c.get("lateralite", "") or {"R": "OD", "L": "OG"}.get(_txt(getattr(v, "laterality", "")), "")
        series.append({"titre": "OCT — B-scans", "oeil": oeil, "orientation": c.get("orientation", ""),
                       "exclue_ir": False, "images": images})
    if any(s["exclue_ir"] for s in series):
        info["pipeline"] = ("Les images infrarouges (IR de repérage des OCT et séries IR seules) sont exclues "
                            "par le pipeline : elles n'iront pas dans images_traitees/.")
        if all(s["exclue_ir"] for s in series):
            info["pipeline"] += " Ce fichier ne contient que de l'IR : statut NON_TRAITEE_IR."
    if not series:
        info["alertes"].append("Aucune image décodable dans ce fichier.")
    return info, series


def analyser_fichier(nom, data):
    """(info JSON-able, séries avec images numpy). Écrit le fichier dans un
    dossier temporaire privé (oct_converter exige un chemin), supprimé ensuite."""
    tmp = tempfile.mkdtemp(prefix="visionneuse_fredd_")
    try:
        typ = type_fichier(nom, data)
        chemin = Path(tmp) / ("fichier.e2e" if typ == "E2E" else "fichier.dcm")
        chemin.write_bytes(data)
        info, series = analyser_e2e(chemin, nom) if typ == "E2E" else analyser_dicom(chemin, nom)
    except Exception as e:  # noqa: BLE001
        info, series = {"erreur": f"Lecture impossible : {type(e).__name__}: {e}", "identifiants": [],
                        "contexte": [], "alertes": []}, []
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    info["nom"] = nom
    info["taille_mo"] = round(len(data) / 1e6, 2)
    info["series"] = [{"titre": s["titre"], "oeil": s["oeil"], "orientation": s["orientation"],
                       "exclue_ir": s["exclue_ir"], "n": len(s["images"]),
                       "dimensions": "×".join(str(x) for x in np.asarray(s["images"][0]).shape[:2][::-1])}
                      for s in series]
    return info, series


class Cache:
    def __init__(self):
        self._d = OrderedDict()
        self._lock = threading.Lock()

    def ajouter(self, series):
        jeton = uuid.uuid4().hex
        with self._lock:
            self._d[jeton] = series
            while len(self._d) > MAX_CACHE:
                self._d.popitem(last=False)
        return jeton

    def image(self, jeton, s, i):
        with self._lock:
            series = self._d.get(jeton)
        if series is None or not (0 <= s < len(series)) or not (0 <= i < len(series[s]["images"])):
            return None
        return series[s]["images"][i]


CACHE = Cache()


class Gestionnaire(BaseHTTPRequestHandler):
    server_version = "VisionneuseFREDD/" + __version__

    def log_message(self, *a):  # pas de journal des requêtes (noms de fichiers)
        pass

    def _envoyer(self, code, corps, type_mime):
        self.send_response(code)
        self.send_header("Content-Type", type_mime)
        self.send_header("Content-Length", str(len(corps)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(corps)

    def do_GET(self):
        u = urlparse(self.path)
        if u.path in ("/", "/index.html"):
            return self._envoyer(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
        if u.path == "/favicon.ico":
            return self._envoyer(204, b"", "image/x-icon")
        if u.path == "/image":
            q = parse_qs(u.query)
            try:
                img = CACHE.image(q["t"][0], int(q["s"][0]), int(q["i"][0]))
            except (KeyError, ValueError, IndexError):
                img = None
            if img is None:
                return self._envoyer(404, b"image inconnue", "text/plain")
            return self._envoyer(200, png(img), "image/png")
        self._envoyer(404, b"introuvable", "text/plain")

    def do_POST(self):
        if urlparse(self.path).path != "/analyser":
            return self._envoyer(404, b"introuvable", "text/plain")
        n = int(self.headers.get("Content-Length", "0"))
        data = self.rfile.read(n)
        nom = unquote(self.headers.get("X-Nom-Fichier", "fichier"))
        info, series = analyser_fichier(nom, data)
        info["jeton"] = CACHE.ajouter(series)
        self._envoyer(200, json.dumps(info, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")


def demarrer(port=8765, navigateur=True):
    for p in range(port, port + 20):
        try:
            srv = ThreadingHTTPServer(("127.0.0.1", p), Gestionnaire)
            break
        except OSError:
            continue
    else:
        sys.exit("Aucun port libre trouvé.")
    url = f"http://127.0.0.1:{srv.server_address[1]}/"
    print(f"Visionneuse FREDD : {url}\n(fermer cette fenêtre ou Ctrl+C pour arrêter ; rien ne sort de ce poste)")
    if navigateur:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


PAGE = r"""<!doctype html>
<html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Visionneuse FREDD</title>
<style>
:root{--fond:#111315;--panneau:#1a1d20;--bord:#2a2e33;--texte:#e6e8ea;--doux:#9aa2ab;--accent:#5eb3a8;
--alerte:#e0a458;--danger:#e07a6f;--ok:#7fbf8e;--rayon:10px}
*{box-sizing:border-box}
body{margin:0;background:var(--fond);color:var(--texte);font:14px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif;
height:100vh;display:grid;grid-template-columns:270px 1fr;overflow:hidden}
aside{border-right:1px solid var(--bord);display:flex;flex-direction:column;min-height:0}
.marque{padding:16px 16px 12px;border-bottom:1px solid var(--bord)}
.marque b{font-size:15px;letter-spacing:.2px}.marque span{display:block;color:var(--doux);font-size:12px;margin-top:2px}
#depot{margin:12px;border:1.5px dashed var(--bord);border-radius:var(--rayon);padding:18px 12px;text-align:center;
color:var(--doux);cursor:pointer;transition:.15s}
#depot:hover,#depot.survol{border-color:var(--accent);color:var(--texte);background:#1d2624}
#liste{overflow:auto;padding:0 8px 12px;flex:1}
.fic{padding:9px 10px;border-radius:8px;cursor:pointer;margin:2px 0;word-break:break-all}
.fic:hover{background:var(--panneau)}.fic.actif{background:#22302d}
.fic small{display:block;color:var(--doux);font-size:11.5px;margin-top:2px}
.pied{padding:10px 14px;border-top:1px solid var(--bord);color:var(--doux);font-size:11.5px}
main{overflow:auto;padding:22px 26px 40px;min-width:0}
.vide{height:100%;display:grid;place-items:center;color:var(--doux);text-align:center}
.vide p{max-width:420px}
h1{font-size:18px;margin:0 0 4px;word-break:break-all}
.sous{color:var(--doux);margin-bottom:16px}
.puces{display:flex;flex-wrap:wrap;gap:6px;margin:10px 0 18px}
.puce{background:var(--panneau);border:1px solid var(--bord);border-radius:999px;padding:3px 10px;font-size:12.5px}
.grille{display:grid;grid-template-columns:minmax(0,1fr) 340px;gap:20px;align-items:start}
@media (max-width:1100px){.grille{grid-template-columns:1fr}}
.carte{background:var(--panneau);border:1px solid var(--bord);border-radius:var(--rayon);padding:14px 16px;margin-bottom:16px}
.carte h2{font-size:13px;text-transform:uppercase;letter-spacing:.6px;color:var(--doux);margin:0 0 10px;font-weight:600}
.message{border-radius:8px;padding:9px 12px;margin:0 0 10px;font-size:13px}
.message.alerte{background:#3a2e1d;color:#f1cf9f}.message.danger{background:#3b2220;color:#f3b2aa}
.message.info{background:#1d2b2a;color:#b7e0d9}.message.ok{background:#1e2d22;color:#b9e1c3}
table{width:100%;border-collapse:collapse;font-size:13px}td{padding:5px 0;vertical-align:top;border-bottom:1px solid var(--bord)}
td:first-child{color:var(--doux);padding-right:10px;width:46%}
.masque{filter:blur(6px);cursor:pointer;user-select:none;transition:.15s}
.masque:hover{filter:blur(4px)}
button.lien{background:none;border:0;color:var(--accent);cursor:pointer;padding:0;font:inherit;font-size:12.5px}
.serie{background:var(--panneau);border:1px solid var(--bord);border-radius:var(--rayon);margin-bottom:16px;overflow:hidden}
.serie header{display:flex;justify-content:space-between;align-items:baseline;gap:10px;padding:11px 14px;border-bottom:1px solid var(--bord)}
.serie header b{font-size:14px}.serie header span{color:var(--doux);font-size:12.5px;white-space:nowrap}
.etiquette{font-size:11px;font-weight:600;border-radius:5px;padding:2px 7px;margin-left:8px;background:#3a2e1d;color:#f1cf9f}
.cadre{background:#000;display:grid;place-items:center;min-height:200px;cursor:zoom-in}
.cadre img{max-width:100%;max-height:62vh;image-rendering:auto;display:block}
.serie.ir .cadre img{opacity:.55}
.controle{display:flex;align-items:center;gap:12px;padding:10px 14px}
.controle input{flex:1;accent-color:var(--accent)}.controle output{color:var(--doux);font-variant-numeric:tabular-nums;min-width:70px;text-align:right}
#zoom{position:fixed;inset:0;background:rgba(0,0,0,.92);display:none;place-items:center;cursor:zoom-out;z-index:9}
#zoom img{max-width:96vw;max-height:94vh}
.charge{color:var(--doux);padding:30px 0}
</style></head><body>
<aside>
  <div class="marque"><b>Visionneuse FREDD</b><span>DICOM et E2E · tout reste sur ce poste</span></div>
  <div id="depot" tabindex="0">Glisser ici un ou plusieurs fichiers<br><small>.dcm, .e2e — ou cliquer pour choisir</small>
    <input id="choix" type="file" multiple hidden></div>
  <div id="liste"></div>
  <div class="pied">Les identifiants sont floutés : cliquer dessus pour les afficher.</div>
</aside>
<main id="vue"><div class="vide"><p>Glisser un fichier DICOM ou E2E n'importe où dans la fenêtre pour voir ses images,
son contenu détecté et les informations identifiantes qu'il contient encore.</p></div></main>
<div id="zoom"><img alt=""></div>
<script>
const fichiers=[];let actif=-1;
const $=s=>document.querySelector(s);
const esc=s=>String(s??"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
function ajouter(liste){for(const f of liste){const i=fichiers.push({nom:f.name,taille:f.size,etat:"lecture",info:null})-1;
 rendreListe();if(actif<0||fichiers.length===1)choisir(i);
 f.arrayBuffer().then(buf=>fetch("/analyser",{method:"POST",headers:{"X-Nom-Fichier":encodeURIComponent(f.name)},body:buf}))
 .then(r=>r.json()).then(info=>{fichiers[i].info=info;fichiers[i].etat="ok";rendreListe();if(actif===i)rendre();})
 .catch(e=>{fichiers[i].etat="erreur";fichiers[i].info={erreur:String(e),identifiants:[],contexte:[],alertes:[],series:[]};rendreListe();if(actif===i)rendre();});}}
function rendreListe(){$("#liste").innerHTML=fichiers.map((f,i)=>{const inf=f.info;let s=f.etat==="lecture"?"lecture…":
 inf&&inf.erreur?"illisible":inf?`${inf.format||""} · ${inf.series.reduce((a,b)=>a+b.n,0)} image(s)`+(inf.identifiants.some(x=>!x.public&&x.valeur)?" · identifiants":""):"";
 return `<div class="fic ${i===actif?"actif":""}" data-i="${i}">${esc(f.nom)}<small>${esc(s)}</small></div>`}).join("");
 document.querySelectorAll(".fic").forEach(el=>el.onclick=()=>choisir(+el.dataset.i));}
function choisir(i){actif=i;rendreListe();rendre();}
function rendre(){const f=fichiers[actif];const v=$("#vue");if(!f){return}
 if(!f.info){v.innerHTML=`<h1>${esc(f.nom)}</h1><div class="charge">Lecture et décodage en cours…</div>`;return}
 const inf=f.info,n=inf.series.reduce((a,b)=>a+b.n,0);
 const idents=inf.identifiants.filter(x=>!x.public&&x.valeur);
 let h=`<h1>${esc(inf.nom)}</h1><div class="sous">${esc(inf.format||"")} · ${inf.taille_mo} Mo · ${n} image(s)</div>`;
 const c=(inf.contenu||[]).filter(x=>!x.erreur);
 if(c.length){h+=`<div class="puces">`+c.map(x=>`<span class="puce">${esc([x.libelle,x.oeil,x.orientation,x.nb_bscans?("×"+x.nb_bscans):""].filter(Boolean).join(" "))}</span>`).join("")+`</div>`}
 h+=`<div class="grille"><div>`;
 if(inf.erreur)h+=`<div class="message danger">${esc(inf.erreur)}</div>`;
 h+=inf.series.map((s,k)=>`<section class="serie ${s.exclue_ir?"ir":""}"><header><b>${esc(s.titre)}${s.exclue_ir?'<span class="etiquette">IR · exclue du pipeline</span>':""}</b>
   <span>${esc([s.oeil,s.orientation,s.dimensions+" px"].filter(Boolean).join(" · "))}</span></header>
   <div class="cadre" data-s="${k}"><img alt="" src="/image?t=${inf.jeton}&s=${k}&i=${Math.floor((s.n-1)/2)}"></div>
   ${s.n>1?`<div class="controle"><input type="range" min="1" max="${s.n}" value="${Math.floor((s.n-1)/2)+1}" data-s="${k}"><output>${Math.floor((s.n-1)/2)+1} / ${s.n}</output></div>`:""}
  </section>`).join("");
 if(!inf.series.length&&!inf.erreur)h+=`<div class="message info">Aucune image affichable dans ce fichier.</div>`;
 h+=`</div><div>`;
 h+=`<div class="carte"><h2>Informations identifiantes</h2>`+(idents.length?
   `<div class="message alerte">${idents.length} information(s) identifiante(s) présente(s) dans le fichier.</div>`:
   `<div class="message ok">Aucune information identifiante dans l'en-tête${inf.format&&inf.format.startsWith("E2E")?" ni dans le bloc identité":""}.</div>`)+
   (inf.identifiants.length?`<table>`+inf.identifiants.map(x=>`<tr><td>${esc(x.libelle)}</td><td>${x.valeur?`<span class="${x.public?"":"masque"}" title="Cliquer pour afficher">${esc(x.valeur)}</span>`:'<i style="color:var(--doux)">vide</i>'}</td></tr>`).join("")+`</table>`+
   (idents.length?`<p style="margin:8px 0 0"><button class="lien" id="tout">Tout afficher</button></p>`:""):"")+
   `<p style="color:var(--doux);font-size:12.5px;margin:10px 0 0">Le texte <b>incrusté dans l'image</b> (nom, date…) n'est pas détecté automatiquement : vérifier à l'œil sur chaque image, en zoomant (clic sur l'image).</p></div>`;
 for(const a of inf.alertes||[])h+=`<div class="message alerte">${esc(a)}</div>`;
 if(inf.pipeline)h+=`<div class="carte"><h2>Dans le pipeline</h2><div style="font-size:13px">${esc(inf.pipeline)}</div></div>`;
 if((inf.contexte||[]).length)h+=`<div class="carte"><h2>Contexte</h2><table>`+inf.contexte.map(x=>`<tr><td>${esc(x.libelle)}</td><td>${esc(x.valeur)}</td></tr>`).join("")+`</table></div>`;
 h+=`</div></div>`;v.innerHTML=h;
 v.querySelectorAll(".masque").forEach(el=>el.onclick=()=>el.classList.remove("masque"));
 const t=$("#tout");if(t)t.onclick=()=>v.querySelectorAll(".masque").forEach(el=>el.classList.remove("masque"));
 v.querySelectorAll('input[type=range]').forEach(r=>{r.oninput=()=>{const k=r.dataset.s;
   v.querySelector(`.cadre[data-s="${k}"] img`).src=`/image?t=${inf.jeton}&s=${k}&i=${r.value-1}`;r.nextElementSibling.value=`${r.value} / ${r.max}`;}});
 v.querySelectorAll(".cadre").forEach(cd=>cd.onclick=()=>{$("#zoom img").src=cd.querySelector("img").src;$("#zoom").style.display="grid";});}
$("#zoom").onclick=()=>$("#zoom").style.display="none";
document.addEventListener("keydown",e=>{if(e.key==="Escape")$("#zoom").style.display="none";});
const depot=$("#depot");depot.onclick=()=>$("#choix").click();$("#choix").onchange=e=>ajouter(e.target.files);
["dragenter","dragover"].forEach(t=>document.addEventListener(t,e=>{e.preventDefault();depot.classList.add("survol")}));
["dragleave","drop"].forEach(t=>document.addEventListener(t,e=>{e.preventDefault();if(t==="drop"||e.target===document.documentElement)depot.classList.remove("survol")}));
document.addEventListener("drop",e=>{if(e.dataTransfer.files.length)ajouter(e.dataTransfer.files)});
</script></body></html>"""


def main(argv=None):
    p = argparse.ArgumentParser(description="Visionneuse locale DICOM / E2E (FREDD).")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--pas-de-navigateur", action="store_true", help="Ne pas ouvrir le navigateur automatiquement.")
    a = p.parse_args(argv)
    demarrer(a.port, not a.pas_de_navigateur)


if __name__ == "__main__":
    main()
