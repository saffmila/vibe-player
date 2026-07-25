"""
Virtual folder definitions for Vibe Player (named collections of file paths).

Loads and saves ``virtual_folders.json`` in the current working directory.
"""

import json
import os

VIRTUAL_FOLDER_JSON = "virtual_folders.json"


def _norm_path(path: str) -> str:
    return os.path.normcase(os.path.normpath(path))


def load_virtual_folders():
    if os.path.exists(VIRTUAL_FOLDER_JSON):
        with open(VIRTUAL_FOLDER_JSON, "r") as file:
            return json.load(file)
    else:
        return {"virtual_folders": {}}


def save_virtual_folders(data):
    with open(VIRTUAL_FOLDER_JSON, "w") as file:
        json.dump(data, file, indent=4)


def add_to_virtual_folder(folder_name, file_path):
    data = load_virtual_folders()
    entries = data.setdefault("virtual_folders", {}).setdefault(folder_name, [])
    key = _norm_path(file_path)
    if not any(_norm_path(p) == key for p in entries):
        entries.append(file_path)
    save_virtual_folders(data)


def remove_from_virtual_folder(folder_name, file_paths):
    """Remove one or more paths from a virtual library (case/slash tolerant)."""
    if isinstance(file_paths, str):
        file_paths = [file_paths]
    data = load_virtual_folders()
    folders = data.get("virtual_folders", {})
    if folder_name not in folders:
        return False
    remove_keys = {_norm_path(p) for p in file_paths if p}
    before = len(folders[folder_name])
    folders[folder_name] = [
        p for p in folders[folder_name] if _norm_path(p) not in remove_keys
    ]
    if len(folders[folder_name]) == before:
        return False
    save_virtual_folders(data)
    return True


def create_virtual_folder(folder_name):
    data = load_virtual_folders()
    if folder_name not in data["virtual_folders"]:
        data["virtual_folders"][folder_name] = []
    save_virtual_folders(data)
