# -*- coding: utf-8 -*-
"""
fredd.py — Point d'entrée unique du pipeline FREDD
==================================================

Les chemins et les colonnes sont écrits UNE FOIS dans config_fredd.ini (créé
au premier lancement, à côté de ce script). Ensuite :

    python fredd.py                 menu interactif
    python fredd.py verifier        Python, dépendances, module pipeline_wp2, configuration
    python fredd.py simulation      inventaire + tri + confrontation, sans traitement (rapide)
    python fredd.py traiter         traitement complet (reprend là où il s'était arrêté)
    python fredd.py visionneuse     visionneuse DICOM / E2E
    python fredd.py tests           suite de tests complète (pytest)
    python fredd.py doublons        d'où viennent les doublons : totaux par centre, sans nom de fichier
    python fredd.py patients        images exploitables par patient : totaux par centre, sans identifiant
    python fredd.py texte           repère les images où du texte est probablement incrusté (--deplacer, --restaurer)
    python fredd.py catalogue       reconstruit la fiche (catalogue_images.csv) de chaque lot d'images traitées
    python fredd.py fovea           repère, dans chaque volume OCT, la coupe passant par la fovéa
    python fredd.py echantillon     tire un échantillon de patients par centre qui couvre les types d'images
                                    (options obligatoires : --patients-par-centre N --min-par-type K --graine G)
    python fredd.py exporter        découpe images_traitees/ en archives ZIP de ~2 Go pour un portail de transfert
                                    (options : --taille-go 1, --date AAAA-MM-JJ, --centre NOM ; reprise automatique)

Sous Windows : double-cliquer sur FREDD.bat ouvre le même menu.
"""

import configparser
import importlib
import platform
import subprocess
import sys
from pathlib import Path

ICI = Path(__file__).resolve().parent
sys.path.insert(0, str(ICI))
CONFIG = ICI / "config_fredd.ini"

MODELE_CONFIG = """\
# Configuration du pipeline FREDD : à remplir une fois, avant le premier lancement.
# Chemins absolus conseillés. Les lignes commençant par # sont des commentaires.

[dossiers]
# Dossier(s) contenant les images : le dossier qui CONTIENT les lots ou les centres
# (jamais directement un dossier de patient). Plusieurs dossiers : séparer par ;
images =
# Export CSV des données FREDD (séparateur ; ou ,)
donnees =
# Dossier de sortie : HORS de tout dossier synchronisé (Seafile, OneDrive...) ; prévoir
# plusieurs dizaines de Go. Garder le MÊME dossier d'un lancement à l'autre : seules
# les nouvelles images sont alors traitées.
sortie =

[colonnes]
# Colonne du pseudonyme patient dans le CSV
identifiant = id
# Colonne du centre (sert au rangement images_traitees/<centre>/)
centre = leg_site_inc_nom
# Colonnes SKEZIA pour la confrontation : « fredd » = préréglage du CRF FREDD
skezia = fredd

[options]
# Images par appel au module pipeline_wp2
taille_paquet = 200
# Traiter seulement un échantillon de patients : chemin de echantillon_patients.csv (voir
# « fredd.py echantillon »). Vide = traiter tout.
echantillon =
# Options supplémentaires de pipeline_fredd.py (voir python pipeline_fredd.py --help)
autres =
"""

DEPENDANCES = [("numpy", "numpy"), ("pydicom", "pydicom"), ("cv2", "opencv-python"),
               ("oct_converter.readers", "oct-converter"), ("pylibjpeg", "pylibjpeg"),
               ("PIL", "pillow")]


class ErreurConfig(Exception):
    pass


def lire_config(chemin=CONFIG, creer=True):
    chemin = Path(chemin)
    if not chemin.is_file():
        if creer:
            chemin.write_text(MODELE_CONFIG, encoding="utf-8")
        raise ErreurConfig(f"Fichier de configuration créé : {chemin}\n"
                           f"  -> l'ouvrir, renseigner images / donnees / sortie, puis relancer.")
    cp = configparser.ConfigParser(interpolation=None, inline_comment_prefixes=None)
    cp.read(chemin, encoding="utf-8-sig")
    g = lambda s, k, d="": (cp.get(s, k, fallback=d) or "").strip()  # noqa: E731

    def chemin_abs(v):
        p = Path(v).expanduser()
        return p if p.is_absolute() else (chemin.parent / p)

    images = [chemin_abs(x.strip()) for x in g("dossiers", "images").split(";") if x.strip()]
    donnees, sortie = g("dossiers", "donnees"), g("dossiers", "sortie")
    manque = [n for n, v in (("images", images), ("donnees", donnees), ("sortie", sortie)) if not v]
    if manque:
        raise ErreurConfig(f"À renseigner dans {chemin.name}, section [dossiers] : {', '.join(manque)}")
    return {
        "images": images, "donnees": chemin_abs(donnees), "sortie": chemin_abs(sortie),
        "identifiant": g("colonnes", "identifiant", "id"), "centre": g("colonnes", "centre"),
        "skezia": g("colonnes", "skezia"), "echantillon": g("options", "echantillon"), "taille_paquet": g("options", "taille_paquet", "200") or "200",
        "autres": g("options", "autres").split(), "fichier": chemin,
    }


def arguments_pipeline(cfg, simulation=False):
    argv = [str(p) for p in cfg["images"]]
    sortie = cfg["sortie"]
    if simulation:  # dossier à part : ne pas mélanger avec l'état de reprise du traitement réel
        sortie = sortie.with_name(sortie.name + "_simulation")
    argv += ["--data", str(cfg["donnees"]), "--sortie", str(sortie), "--col-id", cfg["identifiant"],
             "--taille-paquet", cfg["taille_paquet"]]
    if cfg["centre"]:
        argv += ["--col-centre", cfg["centre"]]
    if cfg["skezia"]:
        argv += ["--cols-skezia", cfg["skezia"]]
    if cfg.get("echantillon"):
        argv += ["--echantillon", str(cfg["echantillon"])]
    if simulation:
        argv.append("--simulation")
    return argv + cfg["autres"]


def _ok(m):
    print(f"  [OK]  {m}")


def _ko(m):
    print(f"  [!!]  {m}")


def verifier(chemin_config=CONFIG):
    """Contrôle tout ce qui peut empêcher un traitement, sans rien traiter."""
    print("Vérification de l'installation FREDD\n")
    bon = True
    v = sys.version_info
    if (3, 10) <= v[:2] < (3, 14):
        _ok(f"Python {platform.python_version()} ({platform.system()})")
    else:
        _ko(f"Python {platform.python_version()} : version 3.10 à 3.13 attendue")
        bon = False
    try:
        import pipeline_fredd as _pf
        _ok(f"pipeline FREDD version {_pf.__version__}")
    except Exception as e:  # noqa: BLE001
        _ko(f"pipeline FREDD inutilisable : {type(e).__name__}: {e}")
        bon = False
    for module, paquet in DEPENDANCES:
        try:
            importlib.import_module(module)
            _ok(f"{paquet}")
        except Exception as e:  # noqa: BLE001
            _ko(f"{paquet} manquant ({type(e).__name__}) -> python -m pip install -r requirements.txt")
            bon = False
    try:
        import pipeline_wp2
        importlib.import_module("pipeline_wp2.main")
        _ok(f"module pipeline_wp2 version {pipeline_wp2.__version__}")
    except Exception as e:  # noqa: BLE001
        _ko(f"module pipeline_wp2 inutilisable : {type(e).__name__}: {e}")
        bon = False
    try:
        cfg = lire_config(chemin_config)
    except ErreurConfig as e:
        _ko(str(e))
        return 1
    for p in cfg["images"]:
        (_ok if p.is_dir() else _ko)(f"dossier d'images : {p}" + ("" if p.is_dir() else " (introuvable)"))
        bon &= p.is_dir()
    if cfg["donnees"].is_file():
        try:
            import pipeline_fredd as pf
            d = pf.DonneesFredd.charger(cfg["donnees"], cfg["identifiant"], cfg["centre"] or None, cfg["skezia"])
            _ok(f"données FREDD : {len(d.patients)} patients (colonne '{d.col_id}', centre '{d.col_centre or '—'}')")
        except Exception as e:  # noqa: BLE001
            _ko(f"données FREDD illisibles : {e}")
            bon = False
    else:
        _ko(f"données FREDD introuvables : {cfg['donnees']}")
        bon = False
    s = cfg["sortie"]
    synchro = [m for m in ("seafile", "onedrive", "dropbox", "google drive") if m in str(s).lower()]
    if synchro:
        _ko(f"sortie dans un dossier synchronisé ({synchro[0]}) : à déplacer (volume, données de santé)")
        bon = False
    else:
        _ok(f"dossier de sortie : {s}")
    print("\n" + ("Tout est prêt." if bon else "À corriger avant de lancer un traitement."))
    return 0 if bon else 1


def simulation(chemin_config=CONFIG):
    import pipeline_fredd as pf
    cfg = lire_config(chemin_config)
    return pf.main(arguments_pipeline(cfg, simulation=True))


def traiter(chemin_config=CONFIG):
    import pipeline_fredd as pf
    cfg = lire_config(chemin_config)
    return pf.main(arguments_pipeline(cfg))


def visionneuse():
    import visionneuse as vz
    vz.main([])
    return 0


def tests():
    return subprocess.call([sys.executable, "-m", "pytest", "-q", str(ICI / "tests")], cwd=str(ICI))


def echantillon(arguments=()):
    """Échantillon de patients par centre (voir echantillon.py), tiré de l'inventaire le plus récent
    (simulation ou traitement réel)."""
    import echantillon as ech
    cfg = lire_config(CONFIG)
    sortie = cfg["sortie"]
    simu = sortie.with_name(sortie.name + "_simulation")
    candidats = [d for d in (sortie, simu) if (d / ech.FICHIER_CONTENU).is_file()]
    if not candidats:
        print(f"[ERREUR] Aucun inventaire trouvé ({sortie / ech.FICHIER_CONTENU}). Lancer d'abord la simulation (menu 2).")
        return 2
    dossier = max(candidats, key=lambda d: (d / ech.FICHIER_CONTENU).stat().st_mtime)
    print(f"Inventaire utilisé : {dossier / ech.FICHIER_CONTENU}")
    return ech.main(["--sortie", str(dossier), *arguments])


def texte(arguments=()):
    """Contrôle du texte incrusté dans les pixels de images_traitees/ (voir controle_texte.py)."""
    import controle_texte as ct
    return ct.main([str(lire_config(CONFIG)["sortie"]), *arguments])


def fovea(arguments=()):
    """Repère la coupe fovéale de chaque volume OCT de images_traitees/ (voir coupe_foveale.py)."""
    import coupe_foveale as cfv
    sortie = lire_config(CONFIG)["sortie"]
    defaut_csv = ["--csv", str(Path(sortie) / "rapports" / "coupe_foveale.csv")]
    args = list(arguments) + ([] if "--csv" in arguments else defaut_csv)
    return cfv.main([str(Path(sortie) / "images_traitees"), *args])


def catalogue(arguments=()):
    """Reconstruit catalogue_images.csv dans chaque lot de images_traitees/ (voir catalogue_images.py)."""
    import catalogue_images as cat
    sortie = lire_config(CONFIG)["sortie"]
    bilan = cat.construire(sortie)
    if bilan is None:
        return 2
    cat.afficher(bilan)
    return 0


def doublons(arguments=()):
    """Totaux de doublons par centre, classés selon la position des deux copies (voir analyse_doublons.py),
    d'après le rapport le plus récent (traitement ou simulation)."""
    import analyse_doublons as ad
    cfg = lire_config(CONFIG)
    sortie = cfg["sortie"]
    simu = sortie.with_name(sortie.name + "_simulation")
    journal = Path("rapports") / "journal_images.csv"
    candidats = [d for d in (sortie, simu) if (d / journal).is_file()]
    if not candidats:
        print(f"[ERREUR] Aucun journal trouvé ({sortie / journal}). Lancer d'abord la simulation (menu 2).")
        return 2
    dossier = max(candidats, key=lambda d: (d / journal).stat().st_mtime)
    print(f"Journal utilisé : {dossier / journal}")
    return ad.main([str(dossier)])


def patients(arguments=()):
    """Images exploitables par patient et par centre (voir analyse_patients.py), d'après le rapport le plus récent."""
    import analyse_patients as ap
    cfg = lire_config(CONFIG)
    sortie = cfg["sortie"]
    simu = sortie.with_name(sortie.name + "_simulation")
    journal = Path("rapports") / "journal_images.csv"
    candidats = [d for d in (sortie, simu) if (d / journal).is_file()]
    if not candidats:
        print(f"[ERREUR] Aucun journal trouvé ({sortie / journal}). Lancer d'abord la simulation (menu 2).")
        return 2
    dossier = max(candidats, key=lambda d: (d / journal).stat().st_mtime)
    print(f"Journal utilisé : {dossier / journal}")
    return ap.main([str(dossier)])


def exporter(arguments=()):
    """Archives ZIP de taille bornée pour le transfert (voir export_images.py)."""
    import export_images as ex
    cfg = lire_config(CONFIG)
    return ex.main(["--sortie", str(cfg["sortie"]), *arguments])


COMMANDES = {
    "verifier": ("Vérifier l'installation et la configuration", verifier),
    "simulation": ("Simulation : inventaire, tri, confrontation (sans traitement)", simulation),
    "traiter": ("Traitement complet (seules les nouvelles images sont traitées)", traiter),
    "visionneuse": ("Ouvrir la visionneuse DICOM / E2E", visionneuse),
    "tests": ("Lancer la suite de tests", tests),
    "exporter": ("Exporter : archives ZIP de ~2 Go pour le transfert (portail de transfert)", exporter),
}


def menu():
    noms = list(COMMANDES)
    print("\n  PIPELINE FREDD\n")
    for i, n in enumerate(noms, 1):
        print(f"   {i}. {COMMANDES[n][0]}")
    print("   0. Quitter\n")
    choix = input("  Votre choix : ").strip()
    if choix in ("", "0"):
        return 0
    try:
        nom = noms[int(choix) - 1]
    except (ValueError, IndexError):
        print("  Choix inconnu.")
        return 2
    print()
    return COMMANDES[nom][1]()


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    try:
        if not argv:
            return menu()
        nom = argv[0].lower().replace("é", "e")
        if nom in ("-h", "--help", "aide"):
            print(__doc__)
            return 0
        if nom == "echantillon":
            return echantillon(argv[1:])
        if nom == "texte":
            return texte(argv[1:])
        if nom == "catalogue":
            return catalogue(argv[1:])
        if nom == "fovea":
            return fovea(argv[1:])
        if nom == "doublons":
            return doublons(argv[1:])
        if nom == "patients":
            return patients(argv[1:])
        if nom not in COMMANDES:
            print(f"Commande inconnue : {argv[0]}\n{__doc__}")
            return 2
        if nom == "exporter":
            return exporter(argv[1:])
        return COMMANDES[nom][1]()
    except ErreurConfig as e:
        print(f"\n{e}")
        return 2


if __name__ == "__main__":
    code = main()
    if len(sys.argv) == 1 and sys.stdin.isatty():  # lancé par double-clic : laisser lire le résultat
        input("\nAppuyer sur Entrée pour fermer.")
    sys.exit(code)
