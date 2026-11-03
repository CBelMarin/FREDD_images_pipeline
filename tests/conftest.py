"""Jeu de données synthétique reproduisant les cas réels rencontrés dans FREDD."""
import hashlib
import io
import os
import sys
import zipfile
from pathlib import Path

import numpy as np
import pytest
import pydicom
from pydicom.dataset import FileDataset, FileMetaDataset
from pydicom.uid import ExplicitVRLittleEndian, generate_uid

RACINE_PROJET = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RACINE_PROJET))
FAKE_RARETIA = Path(__file__).resolve().parent / "fake_raretia"

# Identifiants « réels » présents dans les DICOM sources : ne doivent JAMAIS
# se retrouver dans images_traitees/.
VRAI_NOM = "DUPONT^JEAN"
VRAI_IPP = "IPP987654321"


def dicom_bytes(comments="", frames=1, modality="OP", rgb=True, seed=0, burned=None):
    meta = FileMetaDataset()
    meta.MediaStorageSOPClassUID = "1.2.840.10008.5.1.4.1.1.77.1.5.1"
    meta.MediaStorageSOPInstanceUID = generate_uid()
    meta.TransferSyntaxUID = ExplicitVRLittleEndian
    ds = FileDataset("x", {}, file_meta=meta, preamble=b"\0" * 128)
    ds.SOPClassUID = meta.MediaStorageSOPClassUID
    ds.SOPInstanceUID = meta.MediaStorageSOPInstanceUID
    ds.PatientName = VRAI_NOM
    ds.PatientID = VRAI_IPP
    ds.PatientBirthDate = "19700101"
    ds.Modality = modality
    ds.ImageComments = comments
    if burned:
        ds.BurnedInAnnotation = burned
    ds.Rows = ds.Columns = 4
    ds.SamplesPerPixel = 3 if rgb else 1
    ds.PhotometricInterpretation = "RGB" if rgb else "MONOCHROME2"
    if rgb:
        ds.PlanarConfiguration = 0
    ds.BitsAllocated = ds.BitsStored = 8
    ds.HighBit = 7
    ds.PixelRepresentation = 0
    if frames > 1:
        ds.NumberOfFrames = frames
    rng = np.random.default_rng(seed)
    ds.PixelData = rng.integers(0, 255, size=4 * 4 * (3 if rgb else 1) * frames, dtype=np.uint8).tobytes()
    buf = io.BytesIO()
    ds.save_as(buf, enforce_file_format=True)
    return buf.getvalue()


def e2e_bytes(series, nom_patient="DUPONT", entete_multivolume=False, pixels=False, date_nulle=False,
              oct_illisible=False):
    """Construit un fichier E2E SYNTHÉTIQUE avec les structures binaires
    d'oct_converter. Chaque série : dict(study, serie, motif, en_face, lat,
    lat_bloc (11 par défaut, ou 3), bscans=[(x1,y1,x2,y2), ...], n_oct, n_fond).
    entete_multivolume : préfixe de 64 octets « E2EMultipleVolumeFile »
    (variante gérée par oct_converter : toutes les positions sont décalées).
    Un bloc identité patient (type 9) est ajouté avec `nom_patient` pour
    vérifier qu'il n'est jamais lu ni recopié.

    NB : ceci teste l'analyseur contre la structure supposée par
    oct_converter, pas contre de vrais fichiers Heidelberg."""
    from oct_converter.readers.binary_structs import e2e_binary as b

    def chaine(*textes):
        return b.scan_pattern.build(dict(n_strings=len(textes), string_size=256, text=list(textes)))

    def bscan(x1, y1, x2, y2, n, cx=None, cy=None):
        d = {}
        for sc in b.bscan_metadata.subcons:
            d[sc.name] = [0.0, 0.0] if sc.name == "unknown4" else 0
        for k in ("posX1", "posY1", "posX2", "posY2", "unknown2", "scaley", "unknown3",
                  "centrePosX", "centrePosY", "imgQuality"):
            d[k] = 0.0
        d.update(posX1=x1, posY1=y1, posX2=x2, posY2=y2, numImages=n)
        # Date d'acquisition valide (FILETIME Windows, ici 2023) : avec 0, oct_converter
        # calcule une date antérieure à 1970 et time.gmtime() échoue SOUS WINDOWS
        # (OSError 22), ce qui fait perdre tous les OCT du fichier.
        d["acquisitionTime"] = (1_700_000_000 + 11_644_473_600) * 10_000_000 if not date_nulle else 0
        # cx/cy : permet de simuler le bug d'export réel (posX2/Y2 == posX1/Y1,
        # mais centrePosX/Y correct) trouvé sur des vrais fichiers Heidelberg
        # (voir analyse_contenu.py, lecture du bloc 10004).
        if cx is not None:
            d["centrePosX"] = cx
        if cy is not None:
            d["centrePosY"] = cy
        return b.bscan_metadata.build(d)

    # Hiérarchie (cf. eyepy, e2e_reader.py) : patient (study=series=slice=-1),
    # série (slice=-1 : œil, motif, modalité en face), coupe (slice=2*i : B-scans).
    image_vide = b.image_structure.build(dict(size=0, type=0, unknown=0, height=0, width=0))
    # pixels=True : vraies images (16x16) lisibles par oct_converter, donc par le
    # module pipeline_wp2. Fond : uint8 ; B-scan OCT : uint16.
    rng = np.random.default_rng(7)

    def image(oct_):
        # B-scan OCT non carré (16 x 24, comme un vrai 496 x N) : un bloc carré est une image en face.
        h, w = 16, (24 if oct_ else 16)
        if not pixels:
            return image_vide
        data = (rng.integers(0, 60000, h * w, dtype=np.uint16).tobytes() if oct_
                else rng.integers(0, 255, h * w, dtype=np.uint8).tobytes())
        if oct_ and oct_illisible:
            # Dimensions annoncées (4096x4096) bien plus grandes que les données : oct_converter
            # atteint la fin du fichier, ne peut pas reconstituer le B-scan et le saute.
            return b.image_structure.build(dict(size=len(data), type=0, unknown=0, height=4096, width=4096)) + data
        return b.image_structure.build(dict(size=len(data), type=0, unknown=0, height=h, width=w)) + data
    blocs = [(9, -1, -1, -1, 0, b.patient_id_structure.build(dict(
        first_name="JEAN", surname=nom_patient, title="", birthdate=0, sex="M", patient_id="IPP987654321")))]
    for s in series:
        st, se = s.get("study", 1), s["serie"]
        if s.get("structure_vide"):  # bloc 9005 sans aucun texte : fait échouer read_all_metadata d'oct_converter
            blocs.append((9005, st, se, -1, 0, b.examined_structure.build(dict(n_strings=0, string_size=256, text=[]))))
        if s.get("motif"):
            blocs.append((9006, st, se, -1, 0, chaine(s["motif"], "")))
        if s.get("en_face"):
            blocs.append((9007, st, se, -1, 0, chaine(s["en_face"], s["en_face"])))
        if s.get("lat") and s.get("lat_bloc", 11) == 11:
            blocs.append((11, st, se, -1, 0,
                          b.lat_structure.build(dict(unknown=[0] * 14, laterality=s["lat"], unknown2=0))))
        elif s.get("lat"):
            blocs.append((3, st, se, -1, 0, b.pre_data.build(dict(unknown=0, laterality=s["lat"])) + b"\0" * 91))
        for i, seg in enumerate(s.get("bscans", [])):
            x1, y1, x2, y2 = seg[:4]
            cx, cy = (seg[4], seg[5]) if len(seg) > 4 else (None, None)
            blocs.append((10004, st, se, 2 * i, 0, bscan(x1, y1, x2, y2, len(s["bscans"]), cx, cy)))
        for i in range(s.get("n_oct", 0)):
            blocs.append((1073741824, st, se, 2 * i, 1, image(True)))
        for _ in range(s.get("n_fond", 0)):
            blocs.append((1073741824, st, se, -1, 0, image(False)))
        for _ in range(s.get("n_ind1_8bits", 0)):  # image en face 8 bits rangée avec les coupes (ind 1)
            blocs.append((1073741824, st, se, 0, 1, image(False)))

    zero10 = [0] * 10
    entete = b.header_structure.build(dict(magic1="CMDb", version=0, unknown=zero10))
    dir_off = 36 + 52
    debut_blocs = dir_off + 52 + 44 * len(blocs)
    sous, corps, off = [], b"", debut_blocs
    u32 = lambda v: v & 0xFFFFFFFF  # noqa: E731  (-1 stocké en non signé dans les structures oct_converter)
    for typ, st, se, sl, ind, charge in blocs:
        tete = b.chunk_structure.build(dict(magic3="MDbData", unknown=0, unknown2=0, pos=off, size=len(charge),
                                            unknown3=0, patient_db_id=1, study_id=u32(st), series_id=u32(se),
                                            slice_id=sl, ind=ind, unknown4=0, type=typ, unknown5=0))
        sous.append(b.sub_directory_structure.build(dict(pos=1, start=off, size=len(charge), unknown=0,
                                                         patient_db_id=1, study_id=u32(st), series_id=u32(se),
                                                         slice_id=sl, unknown2=ind, unknown3=0, type=typ,
                                                         unknown4=0)))
        corps += tete + charge
        off += len(tete) + len(charge)
    principal = b.main_directory_structure.build(dict(magic2="MDbMDir", version=0, unknown=zero10, num_entries=0,
                                                      current=dir_off, prev=0, unknown3=0))
    repertoire = b.main_directory_structure.build(dict(magic2="MDbDir", version=0, unknown=zero10,
                                                       num_entries=len(blocs), current=dir_off, prev=0, unknown3=0))
    contenu = entete + principal + repertoire + b"".join(sous) + corps
    return (b"E2EMultipleVolumeFile".ljust(64, b"\0") + contenu) if entete_multivolume else contenu


# OCT OD (volume de 3 lignes horizontales) + autofluorescence OD dans le même E2E
E2E_OCT_AF_OD = dict(series=[
    dict(serie=1, motif="3D Volume", lat="R", bscans=[(0, 0, 10, 0), (0, 1, 10, 1), (0, 2, 10, 2)], n_oct=3, n_fond=1),
    dict(serie=2, en_face="Autofluorescence", lat="R", n_fond=1),
])


def zip_bytes(entries):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in entries.items():
            z.writestr(name, data)
    return buf.getvalue()


def ecrire(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def empreinte_arbre(racine):
    h = {}
    for p in sorted(Path(racine).rglob("*")):
        if p.is_file():
            h[str(p.relative_to(racine))] = (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns)
    return h


DATA_CSV = (
    "id;centre;img_oct;img_retino\n"
    "a1b2c3d4;Centre A;oui;oui\n"        # P1 : images trouvées + déclarées
    "b2c3d4e5;Centre A;non;non\n"        # P2 : images trouvées mais non déclarées
    "c3d4e5f6;Centre B;1;\n"             # P3 : déclaré, aucune image
    "d4e5f6a7;Centre B;;\n"              # P4 : rien
    "e5f6a7b8;Centre B;oui;\n"           # P5 : images dans zip imbriqué (dont une qui plante)
    "f6a7b8c9;Centre A;oui;oui\n"        # P6 : sortie raretia non pseudonymisée
    "a7b8c9d0;Centre B;oui;oui\n"        # P7 : dossier dans Centre A alors que données = Centre B
)


@pytest.fixture
def jeu(tmp_path):
    """Construit l'arborescence de test et renvoie un dict de chemins."""
    racine = tmp_path / "images"
    img1 = dicom_bytes(seed=1)
    # ── Centre A (dossier)
    ecrire(racine / "CentreA" / "a1b2c3d4" / "OD" / "img1.dcm", img1)
    ecrire(racine / "CentreA" / "a1b2c3d4" / "OCT" / "volume.e2e", e2e_bytes(**E2E_OCT_AF_OD))
    ecrire(racine / "CentreA" / "a1b2c3d4" / "photo.jpg", b"\xff\xd8\xff\xe0" + b"\0" * 200)
    ecrire(racine / "CentreA" / "b2c3d4e5" / "multi.dcm", dicom_bytes(frames=2, seed=2))
    ecrire(racine / "CentreA" / "b2c3d4e5" / "sombre.dcm", dicom_bytes(comments="FLAG", seed=3))
    ecrire(racine / "CentreA" / "99999999" / "inconnu.dcm", dicom_bytes(seed=4))   # pseudo hors données
    ecrire(racine / "CentreA" / "divers" / "DUPONT_JEAN.dcm", dicom_bytes(seed=5))  # aucun pseudo
    ecrire(racine / "CentreA" / "f6a7b8c9" / "fuite.dcm", dicom_bytes(comments="LEAK", seed=6))
    ecrire(racine / "CentreA" / "a7b8c9d0" / "mauvais_centre.dcm", dicom_bytes(seed=7))
    ecrire(racine / "CentreA" / "a1b2c3d4" / "notes.txt", b"pas une image")
    # ── Centre B (archive .zip à la racine, avec zip imbriqué)
    inner = zip_bytes({
        "ok.dcm": dicom_bytes(seed=8),
        "plante.dcm": dicom_bytes(comments="CRASH", seed=9),
        "sanspixels.dcm": dicom_bytes(comments="NOPIX", seed=10),
    })
    ecrire(racine / "CentreB.zip", zip_bytes({
        "e5f6a7b8/examen.zip": inner,
        "a1b2c3d4/OD/img1_copie.dcm": img1,         # doublon binaire de Centre A
        "e5f6a7b8/sans_extension": dicom_bytes(seed=11),  # DICOM sans extension
    }))
    # ── Centre C : archive corrompue
    ecrire(racine / "CentreC" / "abimee.zip", b"PK\x03\x04 ceci n'est pas un zip valide")

    data = tmp_path / "data_clean.csv"
    data.write_text(DATA_CSV, encoding="utf-8")
    return {"racine": racine, "data": data, "sortie": tmp_path / "sortie", "tmp": tmp_path}


DATE_TEST = "2026-10-01"  # sous-dossier de date imposé aux lancements de test (reproductible)


def dossier_patient(sortie, centre, pid):
    """images_traitees/<centre>/<date>/<pid>/ quelle que soit la date du passage
    (lancements qui ne fixent pas --date-traitement)."""
    trouves = sorted((sortie / "images_traitees" / centre).glob(f"*/{pid}"))
    assert len(trouves) <= 1, trouves
    return trouves[0] if trouves else sortie / "images_traitees" / centre / DATE_TEST / pid


@pytest.fixture
def lancer():
    import pipeline_fredd

    def _run(jeu, *extra, sortie=None):
        argv = [str(jeu["racine"]), "--data", str(jeu["data"]), "--sortie", str(sortie or jeu["sortie"]),
                "--raretia-path", str(FAKE_RARETIA), "--cols-skezia", "img_oct:oct_bscan,img_retino:fundus",
                "--col-centre", "centre", "--centre-depuis", "dossier", *extra]
        if "--date-traitement" not in extra:
            argv += ["--date-traitement", DATE_TEST]
        return pipeline_fredd.main(argv)
    return _run


def lire_csv(path):
    import csv
    with open(path, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f, delimiter=";"))
