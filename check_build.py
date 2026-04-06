import os

def check_for_junk():
    # Define the build output directory
    build_dir = os.path.join('dist', 'VibePlayer')
    
    # List of "forbidden" extensions that should not be in the release
    # Includes database artifacts like -wal and -shm
    forbidden_extensions = ['.json', '.db', '.db-wal', '.db-shm', '.log', '.prof', '.txt']
    
    # Exceptions - these files are allowed in the build
    allowed_files = ['README.md', 'requirements.txt', 'license.txt']
    
    # Forbidden folder names (like private caches)
    forbidden_folders = ['thumbnail_cache', 'bookmarks']
    
    if not os.path.exists(build_dir):
        print(f"[!] Build folder not found at: {build_dir}")
        return

    print(f"--- Scanning Build: {build_dir} ---")
    found_junk = []

    for root, dirs, files in os.walk(build_dir):
        # 1. Skip internal library directories and AI models
        # These contain their own configs/licenses that are required for the app to run
        ignore_list = ['dist-info', 'site-packages', 'matplotlib', 'torch', 'customtkinter', 'models', 'setuptools']
        if any(lib in root for lib in ignore_list):
            continue

        # 2. Check for forbidden directories (Private Caches)
        for folder in dirs:
            if folder in forbidden_folders:
                path = os.path.join(root, folder)
                found_junk.append(f"FOLDER: {path}")

        # 3. Check for forbidden file extensions
        for file in files:
            ext = os.path.splitext(file)[1].lower()
            
            if ext in forbidden_extensions:
                # Filter out allowed exceptions
                if file not in allowed_files:
                    full_path = os.path.join(root, file)
                    found_junk.append(f"FILE:   {full_path}")

    # Final Report
    if found_junk:
        print("\n[❌] CRITICAL: Found local or private data in build!")
        for item in found_junk:
            print(f"  -> {item}")
        print("\n[!] ACTION REQUIRED: Check your 'VibePlayer.spec' or build process.")
    else:
        print("\n[✅] SUCCESS: Build is clean! No private JSONs, DBs, logs, or caches found.")

if __name__ == "__main__":
    check_for_junk()
