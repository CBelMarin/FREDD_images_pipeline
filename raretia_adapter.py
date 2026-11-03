# -*- coding: utf-8 -*-
"""
raretia_adapter.py
==================

SEUL point de contact entre le pipeline FREDD et le code de prétraitement
(package Python réellement nommé `pipeline_wp2` — "raretia" n'est que
le nom de code utilisé dans ses docstrings/CLI internes). Si l'implémentation
de pipeline_wp2 change, c'est CE fichier, et lui seul, qu'il faut ajuster.

Contrat RÉEL, vérifié par lecture du module pipeline_wp2 ET par une exécution réelle
(DICOM synthétique) :
  - commande : python -m pipeline_wp2.main <entrée> -o <sortie> [options]
    où <entrée> est un dossier (celui du paquet stagé, à plat) ;
  - sortie réelle : pipeline_wp2/main.py crée <sortie>/<stem(entrée)>/, avec
    reports/rapport_complet.csv (et .json) et processed/<patient_id>/*.dcm
    en-dessous. <patient_id> = nom (sans extension) du fichier stagé quand il
    est directement sous <entrée> (notre cas : staging à plat) — c'est donc
    le nom NEUTRE <id_fredd>__<empreinte> que l'on a choisi en étape 3a ;
  - rapport CSV (séparateur ';') : source_file;image_index;image_type;
    original_shape;processed_shape;laterality;angle_of_view;optic_disc;
    fovea;quality_score;quality_issues;borders_cropped;accepted;
    rejection_reason;output_file — accepted vaut "OUI" / "À VÉRIFIER" (libellé de raretia ; côté FREDD : « qualité limitée »),
    output_file est relatif à <sortie>/<stem(entrée)>/ (donc à
    csv_path.parent.parent, PAS à csv_path.parent comme on pourrait le
    supposer puisque reports/ et processed/ sont deux dossiers frères) ;
  - pseudonymisation : PatientName vidé (confirmé OK). PatientID : mis au nom neutre du fichier
    stagé (<id_fredd>__<empreinte>), pour les .e2e COMME pour les .dcm. Le défaut initial (les .dcm
    ressortaient avec PatientID="UNKNOWN", donc en quarantaine) est corrigé dans le code livré
    (pipeline_wp2, reader.py : `metadata["source_file"]`). Le
    contrôle de pseudonymisation de pipeline_fredd.py reste actif pour le détecter si le correctif
    venait à manquer (module pipeline_wp2 remplacé par une version sans les correctifs FREDD).
  - code de retour : 0 même si certains fichiers ne donnent aucune image (le lecteur journalise
    l'erreur et continue) ; 1 seulement si AUCUNE image n'est produite pour le paquet. Un fichier sans
    sortie à code 0 n'est donc pas forcément un plantage : voir le réessai unitaire (etape3_traitement).

Les noms exacts de colonnes du rapport sont désormais connus avec certitude,
mais l'adaptateur garde plusieurs alias (anglais / français) par robustesse
si le module fait évoluer son format : voir COLUMN_ALIASES.
"""

import csv
import re
import os
import shutil
import subprocess
import sys
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path


def _norm_header(h):
    h = unicodedata.normalize("NFKD", str(h)).encode("ascii", "ignore").decode("ascii")
    return h.strip().strip('"').lower().replace(" ", "_").replace("-", "_")


COLUMN_ALIASES = {
    "source": ["source_file", "fichier_source", "source", "input_file", "input", "fichier", "file", "filename"],
    "index": ["image_index", "index", "frame_index", "idx"],
    "image_type": ["image_type", "type_image", "type"],
    "laterality": ["laterality", "lateralite"],
    "score": ["quality_score", "score_qualite", "score"],
    "issues": ["quality_issues", "problemes", "issues"],
    "accepted": ["accepted", "acceptee", "statut", "status"],
    "reason": ["rejection_reason", "motif", "reason", "motif_rejet"],
    "output": ["output_file", "fichier_sortie", "output", "output_path", "sortie"],
}

REQUIRED_KEYS = ("source",)

_TRUE = {"oui", "yes", "true", "1", "accepted", "ok", "vrai"}
_FALSE = {"a_verifier", "a verifier", "non", "no", "false", "0", "rejected", "faux", "flagged"}


def parse_accepted(value):
    """OUI / À VÉRIFIER / True / False... -> True / False / None (inconnu)."""
    if value is None:
        return None
    v = _norm_header(value).replace("_", " ")
    if v in _TRUE:
        return True
    if v in {x.replace("_", " ") for x in _FALSE} or "verif" in v or "limit" in v:
        return False
    return None


def _sniff_delimiter(sample):
    return ";" if sample.count(";") >= sample.count(",") else ","


def read_raretia_report(csv_path):
    """Retourne une liste de dicts normalisés (clés de COLUMN_ALIASES), ou None
    si le CSV n'a pas la forme d'un rapport raretia (pas de colonne source)."""
    try:
        with open(csv_path, "r", encoding="utf-8-sig", errors="replace", newline="") as f:
            sample = f.read(4096)
            f.seek(0)
            reader = csv.reader(f, delimiter=_sniff_delimiter(sample))
            header = next(reader, None)
            if not header:
                return None
            norm = [_norm_header(h) for h in header]
            colmap = {}
            for key, aliases in COLUMN_ALIASES.items():
                for alias in aliases:
                    if alias in norm:
                        colmap[key] = norm.index(alias)
                        break
            if any(k not in colmap for k in REQUIRED_KEYS):
                return None
            rows = []
            for raw in reader:
                if not raw or all(not c.strip() for c in raw):
                    continue
                rows.append({k: (raw[i].strip() if i < len(raw) else "") for k, i in colmap.items()})
            return rows
    except (OSError, csv.Error):
        return None


@dataclass
class SortieRaretia:
    """Une image produite par raretia (une entrée peut en produire plusieurs :
    DICOM multi-frame, volume OCT éclaté en B-scans)."""
    chemin: Path
    image_type: str = ""
    laterality: str = ""
    score: str = ""
    accepted: object = None  # True / False / None
    issues: str = ""
    reason: str = ""
    index: str = ""


@dataclass
class ResultatPaquet:
    returncode: int
    log_path: Path
    duree_s: float
    # nom de fichier stagé (stem) -> liste de sorties
    sorties: dict = field(default_factory=dict)
    rapports_trouves: list = field(default_factory=list)
    message: str = ""


def build_command(entree, sortie, module="pipeline_wp2.main", extra_args=None, python=None):
    cmd = [python or sys.executable, "-m", module, str(entree), "-o", str(sortie)]
    if extra_args:
        cmd.extend(extra_args)
    return cmd


def _env_with_path(raretia_path):
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    if raretia_path:
        prev = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = str(raretia_path) + (os.pathsep + prev if prev else "")
    return env


_PROGRES = re.compile(r"^\s*(\d+)\s*/\s*(\d+)\s+-")  # ligne « i / n  -  fichier » de pipeline_wp2


def _lancer_avec_suivi(cmd, env, cwd, timeout_s, progression):
    """Lance la commande en lisant stdout/stderr au fil de l'eau (au lieu
    d'attendre la fin) : permet d'afficher la progression. Retourne
    (code, stdout, stderr, expiré)."""
    import threading
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            encoding="utf-8", errors="replace", env=env, cwd=cwd, bufsize=1)
    out, err = [], []

    def lire_out():
        for ligne in proc.stdout:
            out.append(ligne)
            m = _PROGRES.match(ligne)
            if m and progression:
                try:
                    progression(int(m.group(1)), int(m.group(2)))
                except Exception:  # noqa: BLE001  (l'affichage ne doit jamais casser le traitement)
                    pass

    def lire_err():
        for ligne in proc.stderr:
            err.append(ligne)

    th = [threading.Thread(target=lire_out, daemon=True), threading.Thread(target=lire_err, daemon=True)]
    for t in th:
        t.start()
    expire = False
    try:
        proc.wait(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        expire = True
        proc.kill()
        proc.wait()
    for t in th:
        t.join(timeout=10)
    return proc.returncode, "".join(out), "".join(err), expire


def run_raretia(entree, sortie, staged_stems, log_path, raretia_path=None,
                module="pipeline_wp2.main", extra_args=None, timeout_s=None, progression=None):
    """Lance raretia sur le dossier `entree`, écrit dans `sortie`, puis
    rattache chaque image produite au fichier stagé dont elle provient.

    staged_stems : ensemble des noms (sans extension) des fichiers stagés.
    progression  : fonction (i, n) appelée quand raretia commence son i-ème
                   fichier (0-based) sur n, pour le suivi à l'écran.
    """
    import time

    sortie = Path(sortie)
    sortie.mkdir(parents=True, exist_ok=True)
    cmd = build_command(entree, sortie, module=module, extra_args=extra_args)
    env = _env_with_path(raretia_path)
    env["PYTHONUNBUFFERED"] = "1"  # sinon les lignes de progression n'arrivent qu'à la fin
    t0 = time.time()
    message = ""
    try:
        # cwd = dossier de sortie : avec « python -m », le dossier courant passe AVANT le
        # PYTHONPATH ; on évite qu'un autre package du même nom y soit importé à la place.
        returncode, out, err, expire = _lancer_avec_suivi(cmd, env, str(sortie), timeout_s, progression)
        if expire:
            returncode = -9
            err += f"\n[TIMEOUT] dépassement de {timeout_s} s"
            message = f"Timeout après {timeout_s} s"
    except OSError as e:
        returncode, out, err = -1, "", f"{type(e).__name__}: {e}"
        message = f"Impossible de lancer raretia : {e}"
    duree = time.time() - t0

    log_path = Path(log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "w", encoding="utf-8") as f:
        f.write("COMMANDE : " + " ".join(cmd) + "\n")
        f.write(f"CODE RETOUR : {returncode}\nDURÉE : {duree:.1f} s\n\n=== STDOUT ===\n{out}\n\n=== STDERR ===\n{err}\n")
    if returncode != 0 and not message:
        last = [l for l in (err or "").strip().splitlines() if l.strip()]
        message = last[-1][:300] if last else f"code retour {returncode}"

    res = ResultatPaquet(returncode=returncode, log_path=log_path, duree_s=duree, message=message)
    _rattacher_sorties(sortie, staged_stems, res)
    return res


def _rattacher_sorties(sortie, staged_stems, res):
    sortie = Path(sortie)
    dcm_files = [p for p in sortie.rglob("*") if p.is_file() and p.suffix.lower() in (".dcm", ".dicom")]
    by_name = {}
    for p in dcm_files:
        by_name.setdefault(p.name, []).append(p)
    attribues = set()
    stems_par_longueur = sorted(staged_stems, key=len, reverse=True)

    # 1) Rattachement via le rapport CSV (source de vérité si présent)
    for csv_path in sorted(sortie.rglob("*.csv")):
        rows = read_raretia_report(csv_path)
        if rows is None:
            continue
        res.rapports_trouves.append(csv_path)
        for r in rows:
            src_stem = Path(r.get("source", "").replace("\\", "/")).stem
            # le champ source peut contenir l'extension, ou un suffixe de frame
            stem = src_stem if src_stem in staged_stems else next(
                (s for s in stems_par_longueur if src_stem.startswith(s)), None)
            if stem is None:
                continue
            out_val = r.get("output", "")
            chemin = None
            if out_val:
                cand = Path(out_val.replace("\\", "/"))
                # output_file est relatif au dossier de sortie RÉEL de pipeline_wp2
                # (<sortie>/<stem(entree)>/), c-à-d csv_path.parent.parent puisque
                # reports/ et processed/ sont deux dossiers frères sous ce dossier
                # (vérifié par exécution réelle). On garde sortie/cand
                # et csv_path.parent/cand en repli, au cas où le module changerait la
                # structure ou le chemin déjà relatif à <sortie>.
                for c in (cand, csv_path.parent.parent / cand, sortie / cand, csv_path.parent / cand):
                    if c.is_file():
                        chemin = c
                        break
                if chemin is None and cand.name in by_name:
                    chemin = by_name[cand.name][0]
            if chemin is None:
                continue  # ligne de rapport sans fichier produit : sera vue comme échec
            attribues.add(chemin.resolve())
            res.sorties.setdefault(stem, []).append(SortieRaretia(
                chemin=chemin, image_type=r.get("image_type", ""), laterality=r.get("laterality", ""),
                score=r.get("score", ""), accepted=parse_accepted(r.get("accepted")),
                issues=r.get("issues", ""), reason=r.get("reason", ""), index=r.get("index", ""),
            ))

    # 2) Repli : DICOM non référencés dans un rapport -> rattachés par préfixe de nom
    for p in dcm_files:
        if p.resolve() in attribues:
            continue
        stem = next((s for s in stems_par_longueur if p.stem.startswith(s)), None)
        if stem is None:
            continue
        res.sorties.setdefault(stem, []).append(SortieRaretia(chemin=p, reason="(non référencé dans le rapport raretia)"))


def version_raretia(raretia_path, module="pipeline_wp2", module_cli=None):
    """Empreinte du code raretia utilisé (traçabilité) : version déclarée si
    disponible + SHA-256 de l'ensemble des fichiers .py du package.

    Sert aussi de contrôle avant traitement : si `module_cli` est fourni, il est
    réellement importé (ce qui charge cv2, numpy, pydicom). Sans cela, un
    environnement sans OpenCV passerait le contrôle puis ferait échouer chaque
    paquet. oct_converter est importé à part : s'il manque, pipeline_wp2 ne
    plante pas mais renvoie 0 image pour chaque .e2e (échec silencieux)."""
    import hashlib
    info = {"chemin": str(raretia_path or "(PYTHONPATH / site-packages)"), "version": "inconnue", "sha256_code": ""}
    script = (f"import {module},os,importlib;print(getattr({module},'__version__','inconnue'));"
              f"print(os.path.dirname({module}.__file__))")
    if module_cli:
        script += f";importlib.import_module({module_cli!r})"
    try:
        proc = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True, text=True, env=_env_with_path(raretia_path), timeout=120,
            cwd=str(raretia_path) if raretia_path else None)
        if proc.returncode == 0:
            lines = proc.stdout.strip().splitlines()
            info["version"] = lines[0]
            pkg_dir = Path(lines[1])
            h = hashlib.sha256()
            for f in sorted(pkg_dir.rglob("*.py")):
                h.update(f.relative_to(pkg_dir).as_posix().encode())
                h.update(f.read_bytes())
            info["sha256_code"] = h.hexdigest()
            info["dossier_package"] = str(pkg_dir)
        else:
            info["erreur"] = (proc.stderr.strip().splitlines() or ["import impossible"])[-1]
            return info
        dep = subprocess.run([sys.executable, "-c", "import oct_converter.readers"],
                             capture_output=True, text=True, timeout=120,
                             env=_env_with_path(raretia_path), cwd=str(raretia_path) if raretia_path else None)
        if dep.returncode != 0:
            info["erreur"] = ("oct-converter non importable : les .e2e ne produiraient aucune image "
                              "(" + ((dep.stderr.strip().splitlines() or ["?"])[-1]) + ")")
    except Exception as e:  # noqa: BLE001
        info["erreur"] = f"{type(e).__name__}: {e}"
    return info
