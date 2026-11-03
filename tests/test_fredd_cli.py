"""Point d'entrée simplifié fredd.py + config_fredd.ini."""
import pytest

import fredd
from conftest import FAKE_RARETIA, dicom_bytes, ecrire, lire_csv

PID = "a1b2c3d4-0000-0000-0000-000000000001"


def _config(tmp_path, **remplace):
    racine = tmp_path / "images"
    ecrire(racine / "CentreA" / PID / f"{PID}_OD.dcm", dicom_bytes(seed=1))
    data = tmp_path / "data.csv"
    data.write_text(f"id;centre;img_retino\n{PID};CentreA;oui\n", encoding="utf-8")
    valeurs = dict(images=str(racine), donnees=str(data), sortie=str(tmp_path / "sortie"),
                   skezia="img_retino:fundus", centre="centre", autres=f"--raretia-path {FAKE_RARETIA}")
    valeurs.update(remplace)
    cfg = tmp_path / "config_fredd.ini"
    cfg.write_text(f"""[dossiers]
images = {valeurs['images']}
donnees = {valeurs['donnees']}
sortie = {valeurs['sortie']}
[colonnes]
identifiant = id
centre = {valeurs['centre']}
skezia = {valeurs['skezia']}
[options]
taille_paquet = 50
autres = {valeurs['autres']}
""", encoding="utf-8")
    return cfg


def test_modele_cree_au_premier_lancement(tmp_path):
    cfg = tmp_path / "config_fredd.ini"
    with pytest.raises(fredd.ErreurConfig, match="créé"):
        fredd.lire_config(cfg)
    assert "[dossiers]" in cfg.read_text(encoding="utf-8")
    with pytest.raises(fredd.ErreurConfig, match="images, donnees, sortie"):
        fredd.lire_config(cfg)


def test_arguments_construits(tmp_path):
    cfg = fredd.lire_config(_config(tmp_path))
    a = fredd.arguments_pipeline(cfg)
    assert a[0].endswith("images") and "--simulation" not in a
    assert a[a.index("--col-centre") + 1] == "centre" and a[a.index("--taille-paquet") + 1] == "50"
    s = fredd.arguments_pipeline(cfg, simulation=True)
    assert "--simulation" in s and s[s.index("--sortie") + 1].endswith("sortie_simulation")


def test_simulation_puis_traitement(tmp_path):
    cfg = _config(tmp_path)
    assert fredd.simulation(cfg) == 0
    assert (tmp_path / "sortie_simulation" / "rapports" / "rapport_execution.md").is_file()
    assert fredd.traiter(cfg) == 0
    (j,) = lire_csv(tmp_path / "sortie" / "rapports" / "journal_images.csv")
    assert j["statut"] == "TRAITEE_OK"


def test_verifier_signale_les_problemes(tmp_path, capsys):
    cfg = _config(tmp_path, images=str(tmp_path / "absent"), sortie=str(tmp_path / "Seafile" / "s"))
    assert fredd.verifier(cfg) == 1
    out = capsys.readouterr().out
    assert "introuvable" in out and "synchronisé" in out and "1 patients" in out


def test_verifier_ok(tmp_path, capsys, monkeypatch):
    # dépendances réduites à celles toujours présentes ici : on teste la logique, pas l'environnement
    monkeypatch.setattr(fredd, "DEPENDANCES", [("numpy", "numpy"), ("pydicom", "pydicom")])
    assert fredd.verifier(_config(tmp_path)) == 0
    assert "Tout est prêt" in capsys.readouterr().out


def test_verifier_affiche_la_version_du_pipeline(capsys, tmp_path):
    import fredd, pipeline_fredd
    fredd.verifier(tmp_path / "absent.ini")
    assert f"pipeline FREDD version {pipeline_fredd.__version__}" in capsys.readouterr().out
