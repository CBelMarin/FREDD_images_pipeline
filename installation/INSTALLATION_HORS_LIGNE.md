# Installer le pipeline sur une machine sans accès Internet

L'environnement cible n'a pas d'accès Internet : tout ce qui est nécessaire est préparé sur un poste connecté, puis importé. Deux voies existent.

| | Voie A : Python portable | Voie B : Python installé + wheels |
| --- | --- | --- |
| Quand | La VM n'a pas Python, ou on ne peut pas en installer | La VM a déjà Python 3.10 à 3.13 |
| Préparation | `installation\preparer_python_portable.ps1` | `installation\preparer_wheels.bat` |
| À importer | `python-portable.zip` + code du dépôt | dossier `wheels\` + code du dépôt |
| Dans l'environnement cible | dézipper, placer `python\` dans le dossier du code | `installation\installer_depuis_wheels.bat` |

Dans les deux cas, `FREDD.bat` trouve tout seul l'interpréteur, dans cet ordre : `python\`, puis `venv\`, puis `%USERPROFILE%\python-3.12.0-embed-amd64\`, puis le Python du système.

## 0. Prérequis à confirmer avec l'administrateur de l'environnement

- Système et version de Python de l'environnement cible (Windows 64 bits, Python 3.10 à 3.13 ; la version 3.12 est celle validée).
- Espace disque : les sorties se comptent en dizaines de Go. Prévoir un disque **local** et un chemin court sans accents.
- Le dossier de sortie doit être **hors** des dossiers synchronisés (Seafile, OneDrive) et hors du dossier d'images scanné.
- Sous Linux uniquement : bibliothèque système `libgl1` (exigée par `opencv-python`).

## 1. Sur un poste connecté : préparer le paquet

### Voie A : Python portable

```bat
git clone <adresse du dépôt>
cd <dossier du dépôt>
powershell -ExecutionPolicy Bypass -File installation\preparer_python_portable.ps1
```

Le script télécharge Python 3.12.0 (version embarquée), active `site-packages`, installe pip puis les dépendances figées de `requirements-lock.txt` et `pytest`, ajoute `sitecustomize.py` (la version embarquée ignore sinon la variable `PYTHONPATH`, ce qui casse l'appel au module pipeline_wp2) et produit `preparation_vm\python-portable.zip`.

### Voie B : wheels

```bat
installation\preparer_wheels.bat
```

À lancer avec **la même version de Python et le même système que l'environnement cible**. Sinon : `pip download -r requirements-lock.txt -d wheels --only-binary=:all: --platform win_amd64 --python-version 3.12`.

### Dans les deux cas : le code

```bat
git archive --format=zip -o fredd_pipeline.zip HEAD
```

L'archive ne contient que les fichiers suivis par git : ni `config_fredd.ini`, ni données, ni images.

## 2. Importer dans l'environnement cible

Importer par le canal autorisé : `fredd_pipeline.zip`, et selon la voie `python-portable.zip` ou le dossier `wheels\` (en zip). Le copier-coller peut ne pas être disponible dans l'environnement cible.

## 3. Installer dans l'environnement cible

### Voie A

1. Dézipper `fredd_pipeline.zip` dans un dossier local, par exemple `D:\FREDD\fredd_pipeline`.
2. Dézipper `python-portable.zip` : il contient un dossier `python`. Le placer **dans** `fredd_pipeline\`, à côté de `FREDD.bat`.

### Voie B

1. Dézipper `fredd_pipeline.zip`, puis placer le dossier `wheels\` à côté de `README.md`.
2. Double-cliquer sur `installation\installer_depuis_wheels.bat` (crée `venv\` et installe sans Internet).

## 4. Vérifier

Double-cliquer sur `installation\verifier_installation.bat`. Il enchaîne la vérification de l'installation puis la suite de tests.

Résultat attendu :

- chaque ligne de la vérification en `[OK]`, avec « pipeline FREDD version 1.12.6 » et « module pipeline_wp2 version 0.1.0+fredd.6 » ;
- la suite de tests : 481 réussis et 2 sautés (le contrôle croisé `eyepy` est sauté si ce paquet n'est pas installé).

## 5. Configurer et lancer

1. Double-cliquer sur `FREDD.bat` : au premier lancement, `config_fredd.ini` est créé (modèle : `config_fredd.exemple.ini`).
2. Renseigner `images` (le dossier qui **contient** les centres), `donnees` (le CSV FREDD) et `sortie`.
3. Dans le menu : **1 Vérifier**, puis **2 Simulation**, puis **3 Traitement**. Voir `README.md`.

## Mise à jour du code

Dézipper la nouvelle archive **par-dessus** le dossier existant. Ne jamais écraser :

- `config_fredd.ini` (chemins propres à l'environnement cible) ;
- le dossier `python\` ou `venv\` ;
- le dossier de sortie (il contient l'état de reprise).

Après une mise à jour, relancer **Vérifier** : la version affichée doit être la nouvelle. La reprise réutilise les anciens résultats même si la version a changé ; pour retraiter, utiliser un dossier de sortie neuf ou l'option `--refaire-tout`.

## Dépannage

| Symptôme | Cause probable | Solution |
| --- | --- | --- |
| `pip install` essaie de compiler `h5py` et échoue (`hdf5.dll`) | Python trop récent (3.14 ou plus) : pas de wheel disponible | Utiliser Python 3.11 à 3.13 |
| `No module named 'pipeline_wp2'` avec Python embarqué | `sitecustomize.py` absent | Le recréer (voir la section « Python embarqué » ci-dessous) |
| « Les chemins UNC ne sont pas pris en charge » | Dossier sur un partage réseau | Les lanceurs utilisent `pushd` ; préférer un disque local |
| `libGL.so.1` introuvable (Linux) | `opencv-python` a besoin de `libgl1` | Demander son installation à l'administrateur |

## Données : ce qui ne doit jamais entrer dans le dépôt

Le `.gitignore` exclut images, DICOM, E2E, tableurs, archives, CSV (sauf `alias_centres.csv`), dossiers de sortie, rapports et `config_fredd.ini`. Les rapports contiennent des chemins sources qui peuvent être identifiants : ils ne quittent jamais l'environnement sécurisé sans relecture.

## Python embarqué : `PYTHONPATH` ignoré

Avec la distribution « embeddable » de Python, le fichier `pythonXY._pth` fait ignorer la variable `PYTHONPATH`, ce qui provoque `ModuleNotFoundError: No module named 'pipeline_wp2'` malgré l'option `--raretia-path`. Correctif : créer `Lib\site-packages\sitecustomize.py` contenant

```python
import os, sys
for p in reversed(os.environ.get("PYTHONPATH", "").split(os.pathsep)):
    if p and p not in sys.path:
        sys.path.insert(0, p)
```

Ce fichier n'est pas nécessaire avec une installation Python standard ; le script `preparer_python_portable.ps1` le crée déjà.
