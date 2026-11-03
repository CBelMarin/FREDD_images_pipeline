# -*- coding: utf-8 -*-
"""
controle_visuel.py : controle visuel du texte incruste sur un echantillon STRATIFIE des DICOM livres.

Trois commandes (tout reste dans l'environnement sécurisé ; aucun chemin, nom ou identifiant n'est affiche) :

  python controle_visuel.py tirer <sortie> [--par-strate 75] [--graine 2026] [--min-strate 200]
      Tire l'echantillon : strates = (fabricant, modalite) ; une strate de moins de --min-strate fichiers
      est fusionnee dans « Autres appareils ». Ecrit <sortie>\\rapports\\controle_visuel_echantillon.csv
      (l'index, la strate et le chemin relatif ; ce fichier ne doit pas sortir de l'environnement sécurisé).

  python controle_visuel.py revue <sortie>
      Ouvre la revue dans le navigateur (http://127.0.0.1:8766). Une image a la fois.
        Espace / 0 : pas de texte        T : texte visible        D : douteux
        <- / -> : precedente / suivante   G : eclaircir (contraste)   Z : taille reelle / ajuste
      Chaque verdict est enregistre aussitot (reprise possible) dans controle_visuel_verdicts.csv.

  python controle_visuel.py bilan <sortie>
      Effectifs seulement : par strate k/n, borne haute unilaterale (Clopper-Pearson, 95 %), total, et borne
      ponderee par les effectifs reels des strates (conservatrice).
"""
import argparse
import csv
import json
import math
import random
import sys
import threading
import warnings
import webbrowser
from collections import Counter, defaultdict
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import numpy as np

warnings.simplefilter("ignore")
try:
    sys.stdout.reconfigure(errors="replace")
except Exception:  # noqa: BLE001
    pass

NOM_ECH = "controle_visuel_echantillon.csv"
NOM_VERD = "controle_visuel_verdicts.csv"
VERDICTS = {"non": "pas de texte", "oui": "texte", "dou": "douteux"}


def _rap(sortie):
    d = Path(sortie) / "rapports"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _lire_csv(p):
    if not p.is_file():
        return []
    with open(p, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f, delimiter=";"))


# ----------------------------------------------------------------------------- tirage
def tirer(sortie, par_strate=75, graine=2026, min_strate=200, log=print):
    import pydicom
    racine = Path(sortie) / "images_traitees"
    fichiers = sorted(racine.rglob("*.dcm"))
    if not fichiers:
        log("aucun DICOM dans images_traitees")
        return 2
    groupes = defaultdict(list)
    for f in fichiers:
        ds = pydicom.dcmread(str(f), stop_before_pixels=True, force=True)
        groupes[(str(ds.get("Manufacturer", "?")).strip() or "?", str(ds.get("Modality", "?")))].append(f)
    strates = defaultdict(list)
    for (fab, mod), liste in groupes.items():
        cle = f"{fab} {mod}" if len(liste) >= min_strate else "Autres appareils"
        strates[cle] += liste
    rng = random.Random(graine)
    lignes, i = [], 0
    for cle in sorted(strates):
        liste = sorted(strates[cle])
        choix = rng.sample(liste, min(par_strate, len(liste)))
        for f in choix:
            lignes.append({"idx": i, "strate": cle, "effectif_strate": len(liste),
                           "chemin_relatif": str(f.relative_to(racine))})
            i += 1
    rng.shuffle(lignes)  # ordre de revue melange : pas de lassitude par strate
    for k, l in enumerate(lignes):
        l["idx"] = k
    with open(_rap(sortie) / NOM_ECH, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["idx", "strate", "effectif_strate", "chemin_relatif"], delimiter=";")
        w.writeheader()
        w.writerows(lignes)
    log(f"echantillon : {len(lignes)} images, graine {graine}")
    for cle in sorted(strates):
        log(f"  {cle} : {min(par_strate, len(strates[cle]))} tirees sur {len(strates[cle])}")
    log("etape suivante : python controle_visuel.py revue <sortie>")
    return 0


# ----------------------------------------------------------------------------- image
def png_depuis_dicom(chemin):
    import cv2
    import pydicom
    ds = pydicom.dcmread(str(chemin), force=True)
    a = ds.pixel_array
    if a.dtype != np.uint8:
        a = a.astype(np.float32)
        lo, hi = float(a.min()), float(a.max())
        a = ((a - lo) / (hi - lo) * 255.0 if hi > lo else a * 0).astype(np.uint8)
    if a.ndim == 3 and a.shape[-1] == 3:
        a = cv2.cvtColor(a, cv2.COLOR_RGB2BGR)
    elif a.ndim == 3:
        a = a[0]
    ok, buf = cv2.imencode(".png", a)
    return buf.tobytes() if ok else b""


# ----------------------------------------------------------------------------- serveur
class Etat:
    def __init__(self, sortie):
        self.sortie = Path(sortie)
        self.ech = _lire_csv(_rap(sortie) / NOM_ECH)
        self.verd = {int(r["idx"]): r["verdict"] for r in _lire_csv(_rap(sortie) / NOM_VERD)}
        self.verrou = threading.Lock()

    def enregistrer(self, i, v):
        with self.verrou:
            self.verd[i] = v
            with open(_rap(self.sortie) / NOM_VERD, "w", encoding="utf-8-sig", newline="") as f:
                w = csv.writer(f, delimiter=";")
                w.writerow(["idx", "strate", "verdict", "date"])
                for k in sorted(self.verd):
                    w.writerow([k, self.ech[k]["strate"], self.verd[k], datetime.now().isoformat(timespec="seconds")])

    def resume(self):
        par = defaultdict(Counter)
        for k, v in self.verd.items():
            par[self.ech[k]["strate"]][v] += 1
        return {"n": len(self.ech), "faites": len(self.verd),
                "premier_restant": next((i for i in range(len(self.ech)) if i not in self.verd), len(self.ech) - 1),
                "verdicts": {str(k): v for k, v in self.verd.items()},
                "compte": {v: sum(1 for x in self.verd.values() if x == v) for v in VERDICTS}}


def fabrique(etat):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):  # silence
            pass

        def _rep(self, code, corps, typ):
            self.send_response(code)
            self.send_header("Content-Type", typ)
            self.send_header("Content-Length", str(len(corps)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(corps)

        def do_GET(self):
            u = urlparse(self.path)
            if u.path == "/":
                self._rep(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
            elif u.path == "/etat":
                self._rep(200, json.dumps(etat.resume()).encode(), "application/json")
            elif u.path == "/info":
                i = int(parse_qs(u.query)["i"][0])
                self._rep(200, json.dumps({"strate": etat.ech[i]["strate"]}).encode("utf-8"), "application/json")
            elif u.path == "/img":
                i = int(parse_qs(u.query)["i"][0])
                try:
                    corps = png_depuis_dicom(etat.sortie / "images_traitees" / etat.ech[i]["chemin_relatif"])
                    self._rep(200, corps, "image/png")
                except Exception:  # noqa: BLE001
                    self._rep(500, b"", "text/plain")
            else:
                self._rep(404, b"", "text/plain")

        def do_POST(self):
            if urlparse(self.path).path == "/verdict":
                d = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
                if d.get("v") in VERDICTS and 0 <= int(d["i"]) < len(etat.ech):
                    etat.enregistrer(int(d["i"]), d["v"])
                    self._rep(200, b"{}", "application/json")
                    return
            self._rep(400, b"", "text/plain")
    return H


def revue(sortie, port=8766, navigateur=True):
    etat = Etat(sortie)
    if not etat.ech:
        print("echantillon absent : lancer d'abord « tirer »")
        return 2
    srv = ThreadingHTTPServer(("127.0.0.1", port), fabrique(etat))
    url = f"http://127.0.0.1:{port}/"
    print(f"Revue : {url}  ({len(etat.verd)}/{len(etat.ech)} deja faites). Ctrl+C pour arreter.")
    if navigateur:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
    return 0


# ----------------------------------------------------------------------------- bilan
def borne_haute(k, n, alpha=0.05):
    """Borne haute unilaterale de Clopper-Pearson (bissection, sans scipy)."""
    if n == 0:
        return float("nan")
    if k >= n:
        return 1.0

    def cdf(p):  # P(X <= k | n, p)
        return sum(math.comb(n, j) * p ** j * (1 - p) ** (n - j) for j in range(k + 1))
    lo, hi = 0.0, 1.0
    for _ in range(60):
        mid = (lo + hi) / 2
        if cdf(mid) > alpha:
            lo = mid
        else:
            hi = mid
    return hi


def bilan(sortie, log=print):
    ech = _lire_csv(_rap(sortie) / NOM_ECH)
    verd = {int(r["idx"]): r["verdict"] for r in _lire_csv(_rap(sortie) / NOM_VERD)}
    if not ech:
        log("echantillon absent")
        return 2
    eff = {}
    cnt = defaultdict(Counter)
    for r in ech:
        eff[r["strate"]] = int(r["effectif_strate"])
        if int(r["idx"]) in verd:
            cnt[r["strate"]][verd[int(r["idx"])]] += 1
    log(f"verdicts : {len(verd)}/{len(ech)}")
    log("strate ; n revues ; texte (k) ; douteux ; borne haute 95 % si k=texte")
    tot_n = tot_k = tot_d = 0
    pond_pt = pond_ub = 0.0
    N = sum(eff.values())
    for s in sorted(eff):
        c = cnt[s]
        n = sum(c.values())
        k, d = c["oui"], c["dou"]
        ub = borne_haute(k, n)
        log(f"{s} ; {n} ; {k} ; {d} ; {ub * 100:.1f} %")
        tot_n, tot_k, tot_d = tot_n + n, tot_k + k, tot_d + d
        if n:
            pond_pt += eff[s] / N * k / n
            pond_ub += eff[s] / N * ub
    log(f"TOTAL ; {tot_n} ; {tot_k} ; {tot_d} ; {borne_haute(tot_k, tot_n) * 100:.1f} % (images groupees, sans ponderation)")
    log(f"estimation ponderee par les effectifs reels : {pond_pt * 100:.2f} % ; borne conservatrice ponderee : {pond_ub * 100:.1f} %")
    log("les « douteux » sont a trancher a part (les compter comme texte donne le cas le plus defavorable).")
    return 0


PAGE = r"""<!doctype html><html lang="fr"><head><meta charset="utf-8"><title>Revue visuelle</title>
<style>
:root{--f:#111315;--t:#e6e8ea;--d:#9aa2ab;--a:#5eb3a8;--w:#e0a458;--r:#e07a6f}
body{margin:0;background:var(--f);color:var(--t);font:14px system-ui,sans-serif;height:100vh;display:flex;flex-direction:column}
header{padding:8px 14px;display:flex;gap:18px;align-items:center;border-bottom:1px solid #2a2e33;flex-wrap:wrap}
#zone{flex:1;overflow:auto;display:flex;justify-content:center;align-items:center}
img{max-width:100%;max-height:100%;object-fit:contain}
img.reel{max-width:none;max-height:none}
img.clair{filter:brightness(2.4) contrast(1.7)}
.b{padding:5px 12px;border:1px solid #2a2e33;border-radius:8px;background:#1a1d20;color:var(--t);cursor:pointer}
.ok{border-color:var(--a)}.tx{border-color:var(--r)}.do{border-color:var(--w)}
#prog{flex:1;min-width:120px;height:6px;background:#2a2e33;border-radius:3px}#bar{height:6px;background:var(--a);border-radius:3px;width:0}
small{color:var(--d)}
</style></head><body>
<header><b id="num"></b><span id="strate"></span><span id="verdict"></span>
<div id="prog"><div id="bar"></div></div><span id="cpt"></span>
<button class="b ok" onclick="mark('non')">Pas de texte (Espace)</button>
<button class="b tx" onclick="mark('oui')">Texte (T)</button>
<button class="b do" onclick="mark('dou')">Douteux (D)</button>
<small>← → naviguer · G éclaircir · Z taille réelle</small></header>
<div id="zone"><img id="im"></div>
<script>
let E=null,i=0;const L={non:"pas de texte",oui:"TEXTE",dou:"douteux"};
async function charger(){E=await (await fetch('/etat')).json();i=E.premier_restant;if(E.faites>=E.n)i=0;aff()}
async function aff(){
 document.getElementById('num').textContent=(i+1)+" / "+E.n;
 const v=E.verdicts[i];document.getElementById('verdict').textContent=v?"→ "+L[v]:"";
 document.getElementById('cpt').textContent=E.faites+" faites · texte "+E.compte.oui+" · douteux "+E.compte.dou;
 document.getElementById('bar').style.width=(100*E.faites/E.n)+"%";
 document.getElementById('im').src='/img?i='+i;
 const inf=await (await fetch('/info?i='+i)).json();document.getElementById('strate').textContent=inf.strate;
 if(i+1<E.n)new Image().src='/img?i='+(i+1);
}
async function mark(v){
 await fetch('/verdict',{method:'POST',body:JSON.stringify({i:i,v:v})});
 E=await (await fetch('/etat')).json();
 if(i+1<E.n)i++;aff();
}
document.addEventListener('keydown',e=>{
 const k=e.key.toLowerCase();
 if(k===' '||k==='0'){e.preventDefault();mark('non')}
 else if(k==='t')mark('oui');else if(k==='d')mark('dou');
 else if(k==='arrowright'&&i+1<E.n){i++;aff()}else if(k==='arrowleft'&&i>0){i--;aff()}
 else if(k==='g')document.getElementById('im').classList.toggle('clair');
 else if(k==='z')document.getElementById('im').classList.toggle('reel');
});
charger();
</script></body></html>"""


def main(argv=None):
    p = argparse.ArgumentParser(description="Controle visuel du texte incruste (echantillon stratifie).")
    sp = p.add_subparsers(dest="cmd", required=True)
    t = sp.add_parser("tirer")
    t.add_argument("sortie")
    t.add_argument("--par-strate", type=int, default=75)
    t.add_argument("--graine", type=int, default=2026)
    t.add_argument("--min-strate", type=int, default=200)
    r = sp.add_parser("revue")
    r.add_argument("sortie")
    r.add_argument("--port", type=int, default=8766)
    r.add_argument("--pas-de-navigateur", action="store_true")
    b = sp.add_parser("bilan")
    b.add_argument("sortie")
    a = p.parse_args(argv)
    if a.cmd == "tirer":
        return tirer(a.sortie, a.par_strate, a.graine, a.min_strate)
    if a.cmd == "revue":
        return revue(a.sortie, a.port, not a.pas_de_navigateur)
    return bilan(a.sortie)


if __name__ == "__main__":
    sys.exit(main())
