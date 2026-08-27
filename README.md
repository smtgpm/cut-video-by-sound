# Video Intro Remover

Outil en deux temps :

1. **`app_extract.py`** — petit IDE Tkinter pour charger une vidéo, marquer un
   ou plusieurs extraits (ex : le générique), et sauvegarder l'audio de ces
   extraits en `.wav`.
2. **`cli_remove.py`** — pour une liste de vidéos, détecte automatiquement où
   apparaît chaque extrait de référence (par similarité audio) et produit une
   copie de chaque vidéo sans ces passages (image + son supprimés).

## Installation

Il faut **ffmpeg** installé sur la machine et accessible dans le PATH
(`ffmpeg -version` doit fonctionner dans un terminal) :

- macOS : `brew install ffmpeg`
- Ubuntu/Debian : `sudo apt install ffmpeg`
- Windows : télécharger un build sur ffmpeg.org et l'ajouter au PATH

Puis les dépendances Python :

```bash
pip install -r requirements.txt
```

## Étape 1 — extraire les extraits de référence

```bash
python app_extract.py
```

- "Ouvrir une vidéo" : choisis l'épisode qui te sert de référence (n'importe
  lequel de la saison).
- Déplace le curseur, clique "Marquer début", déplace le curseur jusqu'à la
  fin du générique, clique "Marquer fin / ajouter extrait".
- Répète pour d'autres extraits si besoin (générique de début, de fin, un
  jingle récurrent, etc.).
- "Extraire l'audio de tous les extraits" : choisis un dossier de sortie —
  chaque extrait est sauvegardé en `.wav` mono 22 050 Hz.

## Étape 2 — nettoyer les autres épisodes

```bash
python cli_remove.py --refs dossier_refs --videos ep01.mp4 ep02.mp4 ep03.mp4 --out dossier_sortie
```

- `--refs` : le dossier de `.wav` créé à l'étape 1.
- `--videos` : la liste des vidéos à nettoyer (tu peux aussi utiliser un glob
  du shell, ex `saison1/*.mp4`).
- `--out` : dossier où sont écrites les vidéos nettoyées
  (`nomdefichier_nettoye.mp4`).

Options :

- `--threshold 0.85` (défaut `0.82`) : seuil de similarité entre 0 et 1.
  - Trop de faux positifs (des passages supprimés à tort) → monte le seuil.
  - Un générique pas détecté → baisse-le (essaie 0.75, 0.7...).
- `--fast-cut` : découpe par copie de flux (ffmpeg `-c copy`), beaucoup plus
  rapide mais la coupe peut être décalée de quelques centaines de ms (pas
  d'accroche sur une keyframe exacte). Sans cette option, ffmpeg ré-encode
  (plus lent, coupe précise à l'image près).

## Comment ça marche (détection)

Chaque extrait de référence et chaque piste audio cible sont convertis en une
séquence de vecteurs "chroma" (empreinte spectrale par petite fenêtre de
temps). Le programme fait glisser la séquence de référence le long de toute
la piste cible et calcule, à chaque position, une similarité cosinus. Les
positions où cette similarité dépasse le seuil sont considérées comme des
occurrences du générique.

Ça marche très bien quand l'audio de référence est (quasiment) le même
enregistrement à chaque fois — ce qui est le cas typique d'un générique de
série. Ça ne fera pas de la reconnaissance "sémantique" (deux musiques
différentes mais similaires ne seront pas confondues, c'est voulu).

## Limites connues

- Le calcul de similarité est en O(durée_vidéo × durée_extrait) ; pour un
  épisode de 45 min et un générique de 30 s, ça reste raisonnable (quelques
  secondes à ~1 min par vidéo), mais plusieurs extraits de référence
  multiplient le temps de traitement d'autant.
- Si le volume du générique varie beaucoup d'un épisode à l'autre (normalisation
  différente), baisse le seuil.
- La détection ne touche que l'audio ; la suppression enlève ensuite le même
  intervalle de temps en vidéo+audio via `remove_segments.py`.
