#!/usr/bin/env python3
import os
import re
import sys
import shutil

try:
    import yaml
    _YAML_OK = True
except ImportError:
    _YAML_OK = False

# -- Configuration --
# ponytail: env override wins; else Z:\Anime on Windows, Plex path on Linux
ROOT       = os.environ.get("ANIME_ROOT") or (r"Z:\Anime" if os.name == "nt" else "/data/media/Plex/Anime")
ALIAS_FILE = os.path.join(ROOT, "aliases.yaml")

def load_aliases():
    """Loads the mapping from original name to canonical name."""
    if not _YAML_OK or not os.path.exists(ALIAS_FILE):
        return {}
    try:
        with open(ALIAS_FILE, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        return {str(k).lower(): str(v) for k, v in data.items()}
    except Exception as e:
        print(f"[!] Error loading aliases: {e}")
        return {}

def load_seasons():
    """Loads optional folder-name -> forced season number overrides.
    AniList has no franchise season index, so download names guess season by
    regex; this file is the reliable source of truth for the cases it gets wrong."""
    path = os.path.join(ROOT, "seasons.yaml")
    if not _YAML_OK or not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        return {str(k).lower(): str(v).zfill(2) for k, v in data.items()}
    except Exception as e:
        print(f"[!] Error loading seasons: {e}")
        return {}

def clean_series_name(name):
    """Removes Season/Part info and illegal chars from a folder name."""
    clean = re.sub(
        r'\s*(?:\b(?:Season|Part)\s*\d+|\bS\d{1,2}\b|\d+(?:nd|rd|th)\s+Season)',
        "", name, flags=re.IGNORECASE
    ).strip()
    return re.sub(r'[\\/*?:"<>|]', "", clean).strip()

def parse_season_and_ep(name):
    """Attempts to find Season and Episode numbers from a filename or folder name."""
    # Look for SNNENN
    match_snnenn = re.search(r'S(\d{1,2})E(\d{1,3})', name, re.IGNORECASE)
    if match_snnenn:
        return match_snnenn.group(1).zfill(2), match_snnenn.group(2).zfill(2)
    
    # Look for Season XX
    match_s = re.search(r'(?:Season|Part)\s*(\d+)', name, re.IGNORECASE)
    s = match_s.group(1).zfill(2) if match_s else "01"
    
    # Look for Episode number (e.g., _08 or - E08)
    match_e = re.search(r'[_-] (\d+)\b|E(\d+)\b|(?<!\d)(\d{1,3})\.(?:mp4|mkv|avi)', name, re.IGNORECASE)
    if match_e:
        e = next(g for g in match_e.groups() if g is not None).zfill(2)
    else:
        e = "01" # Default fallback
    
    return s, e

def consolidate():
    print(f"[*] Starting consolidation in {ROOT}...")
    aliases = load_aliases()
    seasons = load_seasons()

    # Files to ignore/delete
    junk_exts = {'.nfo', '.txt', '.url', '.jpg', '.png'}
    
    for item in os.listdir(ROOT):
        full_path = os.path.join(ROOT, item)
        if not os.path.isdir(full_path) or item.startswith('.'):
            continue
            
        # Determine canonical name + optional forced season for this folder
        canonical = aliases.get(item.lower(), clean_series_name(item))
        forced_season = seasons.get(item.lower())

        # Scan folder for video + subtitle files
        for dirpath, dirnames, filenames in os.walk(full_path):
            for filename in filenames:
                file_path = os.path.join(dirpath, filename)
                name_root, ext = os.path.splitext(filename)
                ext = ext.lower()

                # Delete junk
                if ext in junk_exts:
                    os.remove(file_path)
                    continue

                is_video = ext in {'.mp4', '.mkv', '.avi', '.mov'}
                is_sub   = ext in {'.vtt', '.srt', '.ass', '.ssa'}
                if not (is_video or is_sub):
                    continue

                # Season: forced override wins, else folder/file context.
                s_from_folder, _ = parse_season_and_ep(os.path.basename(dirpath) if "Season" in dirpath else item)
                s_from_file, e = parse_season_and_ep(filename)
                s = forced_season or (s_from_file if 'S' in filename.upper() else s_from_folder)
                e = str(int(e)).zfill(2)  # normalise E004 -> E04 so subs match their video

                # Subtitles keep their language suffix (e.g. ".en") so Jellyfin pairs them.
                lang = ""
                if is_sub:
                    m = re.search(r'\.([a-z]{2,3})$', name_root, re.IGNORECASE)
                    if m:
                        lang = "." + m.group(1).lower()

                target_dir = os.path.join(ROOT, canonical, f"Season {s}")
                new_filename = f"{canonical} - S{s}E{e}{lang}{ext}"
                target_path = os.path.join(target_dir, new_filename)

                if os.path.abspath(file_path) == os.path.abspath(target_path):
                    continue # Already correct

                os.makedirs(target_dir, exist_ok=True)

                if os.path.exists(target_path):
                    # Video collision: keep larger. Subtitle collision: keep existing.
                    if is_video and os.path.getsize(file_path) > os.path.getsize(target_path):
                        os.remove(target_path)
                        shutil.move(file_path, target_path)
                        print(f"[✓] Replaced collision with larger file: {new_filename}")
                    else:
                        os.remove(file_path)
                        print(f"[!] Removed duplicate: {filename}")
                else:
                    shutil.move(file_path, target_path)
                    print(f"[✓] Moved/Renamed: {filename} -> {new_filename}")

    # Final cleanup of empty folders
    print("[*] Cleaning up empty folders...")
    for _ in range(2): # Double pass for nested folders
        for dirpath, dirnames, filenames in os.walk(ROOT, topdown=False):
            if not dirnames and not filenames and dirpath != ROOT:
                try:
                    os.rmdir(dirpath)
                except:
                    pass

    print("[✓] Consolidation Complete.")

if __name__ == "__main__":
    consolidate()
