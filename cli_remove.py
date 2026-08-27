"""
cli_remove.py
Point d'entrée : à partir d'un dossier d'extraits de référence (.wav, ex. les
génériques extraits avec app_extract.py) et d'une liste de vidéos cibles,
détecte les occurrences de chaque référence dans chaque vidéo et produit une
version nettoyée (sans ces passages) de chaque vidéo.

Usage :
    python cli_remove.py --refs dossier_refs --videos ep1.mp4 ep2.mp4 ... --out dossier_sortie

Optimisations disponibles :
    --hw auto|nvenc|qsv|videotoolbox|vaapi|none
        Choix de l'encodeur. "auto" (défaut) détecte le GPU disponible.
    --smart-cut
        Ne ré-encode que quelques secondes autour de chaque coupe et copie le
        flux pour le reste (beaucoup plus rapide). Expérimental, nécessite une
        source H.264.
    --workers N
        Nombre de vidéos traitées en parallèle (défaut : 2). Avec un
        encodeur GPU, la plupart des cartes grand public limitent le nombre
        de sessions d'encodage simultanées (souvent 2-3) : inutile de monter
        beaucoup plus haut dans ce cas. En mode --hw none (CPU), tu peux
        monter --workers jusqu'au nombre de coeurs disponibles.
"""

import argparse
import glob
import os
import shutil
from concurrent.futures import ThreadPoolExecutor, as_completed

from audio_match import find_matches
from remove_segments import remove_segments
from hwaccel import get_encoder_profile, describe, list_available_encoders

VIDEO_EXTENSIONS = {
    ".mp4", ".mov", ".avi", ".mkv", ".webm",
    ".m4v", ".flv", ".wmv", ".mpeg", ".mpg"
}


def process_video(video_path: str, ref_paths, out_dir: str, args) -> str:
    all_segments = []
    for ref in ref_paths:
        matches = find_matches(ref, video_path, threshold=args.threshold)
        for m in matches:
            print(f"[{os.path.basename(video_path)}] [{os.path.basename(ref)}] "
                  f"match {m.start:.2f}s -> {m.end:.2f}s (score={m.score:.2f})")
            all_segments.append((m.start, m.end))

    base = os.path.splitext(os.path.basename(video_path))[0]
    out_path = os.path.join(out_dir, f"{base}_nettoye.mp4")

    if all_segments:
        remove_segments(
            video_path, all_segments, out_path,
            hw_prefer=args.hw, vaapi_device=args.vaapi_device,
            smart_cut=args.smart_cut,
        )
    else:
        print(
            f"[{os.path.basename(video_path)}] Aucune occurrence trouvée, copie telle quelle.")
        shutil.copy(video_path, out_path)

    print(f"[{os.path.basename(video_path)}] -> écrit : {out_path}")
    return out_path


def main():
    parser = argparse.ArgumentParser(
        description="Supprime des passages vidéo+audio (ex: génériques) d'une liste de vidéos, "
                    "par similarité avec des extraits audio de référence."
    )
    parser.add_argument("--refs", required=True,
                        help="Dossier contenant les .wav de référence")
    parser.add_argument("--videos", nargs="+", required=True,
                        help="Vidéos cibles à nettoyer")
    parser.add_argument("--out", required=True, help="Dossier de sortie")
    parser.add_argument("--threshold", type=float,
                        default=0.82, help="Seuil de similarité (0-1)")
    parser.add_argument(
        "--hw", default="auto",
        help="Encodeur : auto (défaut, détecte le GPU), nvenc, qsv, videotoolbox, vaapi, ou none (CPU)",
    )
    parser.add_argument(
        "--vaapi-device", default="/dev/dri/renderD128", help="Périphérique VAAPI (Linux)")
    parser.add_argument(
        "--smart-cut", action="store_true",
        help="Expérimental : ne ré-encode que le voisinage des coupes, copie le flux pour le reste",
    )
    parser.add_argument("--workers", type=int, default=2,
                        help="Nombre de vidéos traitées en parallèle (défaut 2)")
    args = parser.parse_args()

    ref_paths = sorted(glob.glob(os.path.join(args.refs, "*.wav")))
    if not ref_paths:
        raise SystemExit(f"Aucun .wav trouvé dans {args.refs}")

    os.makedirs(args.out, exist_ok=True)

    profile = get_encoder_profile(args.hw, args.vaapi_device)
    detected = list_available_encoders(args.vaapi_device)
    print(f"Encodeurs GPU détectés par ffmpeg : {detected or 'aucun'}")
    print(f"Encodeur utilisé : {describe(profile)}")
    if args.smart_cut:
        print("Mode smart-cut activé (expérimental).")
    print()

    videos = []
    for path in args.videos:
        if os.path.isdir(path):
            # Take all videos directly inside the folder
            folder_videos = [
                os.path.join(path, filename)
                for filename in os.listdir(path)
                if os.path.isfile(os.path.join(path, filename))
                and os.path.splitext(filename)[1].lower() in VIDEO_EXTENSIONS
            ]

            videos.extend(sorted(folder_videos))
        else:
            videos.append(path)

    if not videos:
        print("Aucune vidéo trouvée.")
        return

    errors = []

    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as executor:
        futures = {
            executor.submit(process_video, video, ref_paths, args.out, args): video
            for video in videos
        }

        for future in as_completed(futures):
            video = futures[future]
            try:
                future.result()
            except Exception as exc:
                errors.append((video, str(exc)))
                print(f"[{os.path.basename(video)}] ERREUR : {exc}")

    if errors:
        print(f"\n{len(errors)} vidéo(s) en erreur sur {len(videos)}.")
    else:
        print(f"\nTerminé : {len(videos)} vidéo(s) traitée(s) sans erreur.")


if __name__ == "__main__":
    main()
