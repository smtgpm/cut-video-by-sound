# Video Intro Remover

## Fonctionnement

Le programme détecte les extraits audio de référence puis supprime les passages correspondants des vidéos.

### Aucune perte de qualité

La suppression utilise exclusivement FFmpeg avec `-c copy` :

- aucune vidéo n'est ré-encodée ;
- aucun audio n'est ré-encodé ;
- NVENC/QuickSync/VAAPI/VideoToolbox ne sont pas utilisés ;
- les flux conservés restent donc tels quels.

Les limites des morceaux sont alignées sur les **keyframes**. La coupe peut donc être légèrement décalée par rapport au timestamp détecté, mais vidéo et audio restent copiés ensemble.

## Installation

Installer FFmpeg et FFprobe, puis :

```bash
pip install -r requirements.txt
```

## 1. Créer les références audio

```bash
python app_extract.py
```

Marquer le début et la fin du générique, puis exporter les extraits `.wav`.

## 2. Nettoyer les vidéos

Fichiers individuels :

```bash
python cli_remove.py --refs dossier_refs --videos ep01.mp4 ep02.mp4 --out dossier_sortie
```

Dossier entier :

```bash
python cli_remove.py --refs dossier_refs --videos saison1 --out dossier_sortie
```

Plusieurs dossiers/fichiers peuvent être mélangés :

```bash
python cli_remove.py --refs refs --videos saison1 saison2 special.mp4 --out sortie
```

Les dossiers sont parcourus un niveau : seuls les fichiers vidéo directement dedans sont pris.

## Options

- `--threshold 0.82` : seuil de similarité.
- `--workers 2` : nombre de vidéos traitées en parallèle.

Les anciennes options GPU/smart-cut ne sont plus nécessaires : le pipeline est désormais toujours en copie de flux.
