# -*- coding: utf-8 -*-
"""
export_images.py — Découpe images_traitees/ en archives ZIP de taille bornée, pour transférer
par un portail de transfert (ou tout autre) sans tout refaire si un envoi échoue.
=====================================================================================

Pourquoi : un envoi de plusieurs Go est long ; s'il est interrompu (session fermée, limite de
connexion, coupure de l'un ou l'autre côté), il faut tout recommencer. Ici, chaque archive est
petite (2 Go par défaut) : une coupure ne fait perdre qu'une archive.

    python fredd.py exporter                       archives dans <sortie>/export/
    python fredd.py exporter --taille-go 1         archives de ~1 Go
    python fredd.py exporter --date 2026-10-16     seulement le dossier de date voulu
    python fredd.py exporter --centre Lille        seulement ce centre
    python export_images.py --controler DOSSIER    vérifier les archives reçues (somme SHA-256)

Ce qui est fait :
  - une archive = un (centre, date de traitement), nommée fredd_<centre>_<date>_partNNN.zip ;
    les chemins à l'intérieur sont ceux de images_traitees/ (<centre>/<date>/<id>/fichier.dcm) :
    dézipper plusieurs archives dans le même dossier reconstitue l'arborescence ;
  - export/manifest_export.csv : archive, nombre de fichiers, taille, SHA-256 ;
    export/contenu_export.csv : fichier -> archive (mémoire de ce qui a déjà été exporté) ;
  - REPRISE : relancer la commande n'archive que les fichiers pas encore exportés (nouvelles
    images, ou archives non terminées) ; supprimer une archive déjà transférée est sans
    conséquence. Pour la refaire : --refaire ;
  - une archive est écrite en .partiel puis renommée : une archive terminée est toujours complète.

Ce qui n'est PAS exporté : rapports/, sans_patient/, non_traitees_format/, echecs_traitement/,
quarantaine_pseudonymisation/. Les rapports contiennent le chemin des fichiers sources
(chemin_source), qui peut porter un nom identifiant : ne pas les sortir sans les relire.

La taille d'une archive est celle APRÈS compression (ZIP_DEFLATED) ; elle peut dépasser la limite
d'un fichier au plus. Les DICOM produits par le pipeline ne sont pas compressés : la compression
réduit souvent fortement le volume à transférer (le rapport final indique le gain mesuré).
"""

import argparse
import csv
import hashlib
import os
import re
import sys
import zipfile
from pathlib import Path

ENTETE_MANIFEST = ["archive", "centre", "date", "nb_fichiers", "taille_octets", "sha256"]
ENTETE_CONTENU = ["archive", "fichier", "taille_octets"]
FICHIER_MANIFEST = "manifest_export.csv"
FICHIER_CONTENU = "contenu_export.csv"


def _sha256(path, bloc=1 << 20):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(bloc), b""):
            h.update(b)
    return h.hexdigest()


def _lire_csv(path):
    path = Path(path)
    if not path.is_file():
        return []
    with open(path, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f, delimiter=";"))


def _ecrire_csv(path, entete, lignes):
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(entete)
        for r in lignes:
            w.writerow([r.get(h, "") for h in entete])
    os.replace(tmp, path)


def _sain(texte):
    return re.sub(r"[^\w.-]+", "_", str(texte)).strip("_") or "x"


NOM_CATALOGUE = "catalogue_images.csv"


def _groupes(images_traitees, centre=None, date=None):
    """{(centre, date): [fichiers triés]} d'après images_traitees/<centre>/<date>/…"""
    res = {}
    for c in sorted(p for p in Path(images_traitees).iterdir() if p.is_dir()):
        if centre and c.name != centre:
            continue
        for d in sorted(p for p in c.iterdir() if p.is_dir()):
            if date and d.name != date:
                continue
            res[(c.name, d.name)] = sorted(f for f in d.rglob("*") if f.is_file())
    return res


def _prochain_numero(manifest, centre, date):
    n = 0
    for l in manifest:
        if l["centre"] == centre and l["date"] == date:
            m = re.search(r"_part(\d+)\.zip$", l["archive"])
            if m:
                n = max(n, int(m.group(1)))
    return n + 1


def exporter(sortie, taille_max_go=2.0, date=None, centre=None, compresser=True,
             destination=None, refaire=False, log=print):
    """Crée les archives manquantes. Retourne 0 (ok), 2 (rien à exporter / erreur d'entrée)."""
    src = Path(sortie) / "images_traitees"
    if not src.is_dir():
        log(f"[ERREUR] Dossier introuvable : {src} (aucune image traitée ?)")
        return 2
    if taille_max_go <= 0:
        log("[ERREUR] La taille maximale d'une archive doit être positive.")
        return 2
    dest = Path(destination) if destination else Path(sortie) / "export"
    dest.mkdir(parents=True, exist_ok=True)
    for reste in dest.glob("*.partiel"):  # archive interrompue : jamais conservée
        reste.unlink()
    limite = int(taille_max_go * 1e9)
    manifest = [] if refaire else _lire_csv(dest / FICHIER_MANIFEST)
    contenu = [] if refaire else _lire_csv(dest / FICHIER_CONTENU)
    deja = {l["fichier"] for l in contenu}
    groupes = _groupes(src, centre, date)
    if not groupes:
        log("[INFO] Aucun dossier (centre / date) correspondant dans images_traitees/.")
        return 2

    nb_archives = brut = comprime = 0
    for (c, d), fichiers in groupes.items():
        catalogues = [f for f in fichiers if f.name == NOM_CATALOGUE]
        restants = [f for f in fichiers if f.name != NOM_CATALOGUE and f.relative_to(src).as_posix() not in deja]
        if restants:
            restants = catalogues + restants          # le catalogue (réécrit à chaque passage) part avec chaque lot
        if not restants:
            log(f"  {c} / {d} : déjà exporté ({len(fichiers)} fichier(s))")
            continue
        n = _prochain_numero(manifest, c, d)
        i = 0
        while i < len(restants):
            nom = f"fredd_{_sain(c)}_{_sain(d)}_part{n:03d}.zip"
            tmp = dest / (nom + ".partiel")
            membres = []
            with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED if compresser else zipfile.ZIP_STORED,
                                 compresslevel=6 if compresser else None) as zf:
                while i < len(restants):
                    f = restants[i]
                    zf.write(f, arcname=f.relative_to(src).as_posix())
                    membres.append(f)
                    i += 1
                    if zf.fp.tell() >= limite:
                        break
            final = dest / nom
            os.replace(tmp, final)
            taille = final.stat().st_size
            manifest.append({"archive": nom, "centre": c, "date": d, "nb_fichiers": len(membres),
                             "taille_octets": taille, "sha256": _sha256(final)})
            for f in membres:
                contenu.append({"archive": nom, "fichier": f.relative_to(src).as_posix(),
                                "taille_octets": f.stat().st_size})
            _ecrire_csv(dest / FICHIER_CONTENU, ENTETE_CONTENU, contenu)   # état écrit après CHAQUE archive
            _ecrire_csv(dest / FICHIER_MANIFEST, ENTETE_MANIFEST, manifest)
            nb_archives += 1
            brut += sum(f.stat().st_size for f in membres)
            comprime += taille
            log(f"  {nom} : {len(membres)} fichier(s), {taille / 1e6:.1f} Mo")
            n += 1
    if nb_archives:
        gain = f" (volume réduit à {100 * comprime / brut:.0f} % de l'original)" if compresser and brut else ""
        log(f"\n{nb_archives} archive(s) créée(s) dans {dest} : {comprime / 1e6:.1f} Mo au lieu de "
            f"{brut / 1e6:.1f} Mo{gain}.")
        log(f"Liste et sommes de contrôle : {dest / FICHIER_MANIFEST}")
    else:
        log("\nRien de nouveau à exporter.")
    return 0


def controler(dossier, log=print):
    """Compare chaque archive du manifeste à sa taille et à sa somme SHA-256. 0 si tout est bon."""
    dossier = Path(dossier)
    manifest = _lire_csv(dossier / FICHIER_MANIFEST)
    if not manifest:
        log(f"[ERREUR] {FICHIER_MANIFEST} introuvable ou vide dans {dossier}")
        return 2
    mauvais = 0
    for l in manifest:
        p = dossier / l["archive"]
        if not p.is_file():
            log(f"  [!!] MANQUANTE  {l['archive']}")
            mauvais += 1
        elif p.stat().st_size != int(l["taille_octets"]):
            log(f"  [!!] TAILLE     {l['archive']} ({p.stat().st_size} octets au lieu de {l['taille_octets']})")
            mauvais += 1
        elif _sha256(p) != l["sha256"]:
            log(f"  [!!] SOMME      {l['archive']} (contenu différent : à renvoyer)")
            mauvais += 1
        else:
            log(f"  [OK] {l['archive']}")
    inconnues = sorted(p.name for p in dossier.glob("*.zip") if p.name not in {l["archive"] for l in manifest})
    for nom in inconnues:
        log(f"  [??] {nom} : absente du manifeste")
    log(f"\n{len(manifest) - mauvais}/{len(manifest)} archive(s) conforme(s)." +
        ("" if not mauvais else " À renvoyer : celles marquées [!!]."))
    return 0 if not mauvais else 1


def main(argv=None):
    p = argparse.ArgumentParser(description="Archives ZIP de taille bornée pour transférer images_traitees/.")
    p.add_argument("--sortie", help="Dossier de sortie du pipeline (celui qui contient images_traitees/).")
    p.add_argument("--taille-go", type=float, default=2.0, help="Taille maximale approximative d'une archive, en Go (2).")
    p.add_argument("--date", help="Seulement ce dossier de date de traitement (AAAA-MM-JJ).")
    p.add_argument("--centre", help="Seulement ce centre (nom du dossier).")
    p.add_argument("--sans-compression", action="store_true", help="Stocker sans compresser (plus rapide, plus gros).")
    p.add_argument("--destination", help="Dossier des archives (défaut : <sortie>/export).")
    p.add_argument("--refaire", action="store_true", help="Ignorer ce qui a déjà été exporté.")
    p.add_argument("--controler", metavar="DOSSIER", help="Vérifier les archives d'un dossier d'après son manifeste.")
    a = p.parse_args(argv)
    if a.controler:
        return controler(a.controler)
    if not a.sortie:
        p.error("--sortie est obligatoire (sauf avec --controler)")
    return exporter(a.sortie, a.taille_go, a.date, a.centre, not a.sans_compression, a.destination, a.refaire)


if __name__ == "__main__":
    sys.exit(main())
