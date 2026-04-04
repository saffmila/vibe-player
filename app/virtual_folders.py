"""
Virtual folder definitions for Vibe Player (named collections of file paths).

Loads and saves ``virtual_folders.json`` in the current working directory.
"""

import json
import os
import logging

VIRTUAL_FOLDER_JSON = "virtual_folders.json"

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
    if folder_name in data["virtual_folders"]:
        if file_path not in data["virtual_folders"][folder_name]:
            data["virtual_folders"][folder_name].append(file_path)
    else:
        data["virtual_folders"][folder_name] = [file_path]
    save_virtual_folders(data)

def create_virtual_folder(folder_name):
    data = load_virtual_folders()
    if folder_name not in data["virtual_folders"]:
        data["virtual_folders"][folder_name] = []
    save_virtual_folders(data)
