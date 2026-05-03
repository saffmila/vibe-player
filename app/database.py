"""
SQLite-backed media catalog for Vibe Player.

Stores file paths, thumbnails, ratings, keywords, cache flags, and duration; supports
normalized search, path updates, and an in-memory row cache for hot paths.
"""

import os
import dataset
import unicodedata
import logging
from collections import Counter

from sqlalchemy.pool import NullPool

from vtp_constants import IMAGE_FORMATS, VIDEO_FORMATS

_ENTRY_CACHE_MAXSIZE = 8_000

# SQLite + dataset: each OS thread caches one SQLAlchemy Connection (see dataset.Database.executable).
# A bounded QueuePool caused checkout timeouts under ~32 thumbnail workers + DnD + preview threads.
# NullPool creates a fresh DBAPI connection per checkout (no pool starvation); WAL remains on via dataset.
_SQLITE_ENGINE_KWARGS = {
    "poolclass": NullPool,
    "connect_args": {"timeout": 60},
}


class Database:
    def __init__(self, db_name="catalog.db"):
            base_dir = os.path.dirname(os.path.abspath(__file__))
            full_path = os.path.join(base_dir, db_name)

            self.db = dataset.connect(
                f"sqlite:///{full_path}",
                engine_kwargs=_SQLITE_ENGINE_KWARGS,
            )
            self.table = self.db['files']

            # In-memory row cache: normalized_path → row dict (or None if not in DB).
            # Capped at _ENTRY_CACHE_MAXSIZE to prevent unbounded memory growth.
            self._entry_cache: dict[str, dict | None] = {}

            # 1. Unified on 'file_path' (fixed from 'path')
            if 'file_path' not in self.table.columns:
                self.table.create_column('file_path', dataset.types.String)
            if 'is_cached' not in self.table.columns:
                self.table.create_column('is_cached', dataset.types.Boolean)
                
                # Ensure the duration column exists in the files table
            if 'duration' not in self.table.columns:
                self.table.create_column('duration', dataset.types.Float)
                
            # 2. Index on correct column
            self.db.query("CREATE INDEX IF NOT EXISTS idx_file_path ON files (file_path)")

            # 3. Loading from correct column 'file_path'
            try:
                # Here was the error - row['path'] vs row['file_path']
                self._cached_paths_set = {row['file_path'] for row in self.table.find(is_cached=True) if row['file_path']}
                logging.info(f"State cache loaded: {len(self._cached_paths_set)} items.")
            except Exception as e:
                logging.error(f"Error filling cache: {e}")
                self._cached_paths_set = set()

    # ------------------------------------------------------------------
    # Row-level entry cache helpers
    # ------------------------------------------------------------------

    def _get_cached_entry(self, file_path: str) -> dict | None:
        """Return DB row for file_path, using in-memory cache to avoid repeat queries.
        Automatically evicts the entire cache when it exceeds _ENTRY_CACHE_MAXSIZE."""
        norm = self.normalize_path(file_path)
        if norm not in self._entry_cache:
            if len(self._entry_cache) >= _ENTRY_CACHE_MAXSIZE:
                self._entry_cache.clear()
                logging.debug(f"Entry cache evicted (reached {_ENTRY_CACHE_MAXSIZE} entries).")
            self._entry_cache[norm] = self.table.find_one(file_path=norm)
        return self._entry_cache[norm]

    def _invalidate_cache(self, file_path: str) -> None:
        """Remove a single path from the row cache so next read hits DB fresh."""
        self._entry_cache.pop(self.normalize_path(file_path), None)

    def clear_entry_cache(self) -> None:
        """Flush the entire row cache — call this on folder Refresh."""
        self._entry_cache.clear()
            

    
    def add_entry(self, filename, file_path, width, height, rating=0, keywords="", is_cached=False, thumbnail_time=None):
        """
        Add a new file entry to the database with normalized file paths and filenames.
        """
        try:
            file_path_normalized = self.normalize_path(file_path)
            filename_normalized = filename.strip().lower()

            # Use row cache to avoid a DB round-trip for already-known files
            if self._get_cached_entry(file_path_normalized) is not None:
                return

            new_row = dict(
                filename=filename_normalized,
                file_path=file_path_normalized,
                width=width,
                height=height,
                keywords=keywords,
                rating=rating,
                is_cached=is_cached,
                thumbnail_timestamp=thumbnail_time,
            )
            self.table.insert(new_row)
            # Warm the cache with the newly inserted row
            self._entry_cache[file_path_normalized] = new_row

        except Exception as e:
            logging.error(f"Error inserting entry for {file_path}: {e}")



    def set_thumbnail_timestamp(self, file_path, timestamp):
        norm = self.normalize_path(file_path)
        self.table.upsert(
            dict(file_path=norm, thumbnail_timestamp=timestamp),
            keys=["file_path"],
        )
        self._invalidate_cache(norm)


    def update_file_metadata(self, file_path, **kwargs):
        """Universally updates any metadata for a file in the database."""
        file_path_normalized = self.normalize_path(file_path)
        data = dict(file_path=file_path_normalized)
        data.update(kwargs)
        self.table.upsert(data, keys=['file_path'])
        self._invalidate_cache(file_path_normalized)

    def get_single_thumbnail(self, video_path):
        if not video_path:
            return None
        try:
            entry = self._get_cached_entry(os.path.abspath(video_path))
            if entry and "thumbnail_timestamp" in entry:
                return {"timestamp": entry["thumbnail_timestamp"], "image_path": None}
        except Exception as e:
            logging.error(f"get_single_thumbnail failed: {e}")
        return None




    def get_all_keywords(self):
        with self.db as db:
            result = db.query("SELECT DISTINCT keywords FROM files")
            keywords = set()
            for row in result:
                if row['keywords']:
                    keywords.update(row['keywords'].split(','))
            return list(keywords)
    
    def get_keywords(self, file_path):
        result = self._get_cached_entry(file_path)
        return result['keywords'] if result else 'No keywords'

    
    def get_rating(self, file_path):
        try:
            record = self._get_cached_entry(file_path)
            return record.get('rating', 0) if record else None
        except Exception as e:
            logging.error(f"Failed to retrieve rating for {file_path}: {e}")
            return None

        
        
    def get_entry(self, file_path):
        return self._get_cached_entry(file_path)


    def update_rating(self, file_path, rating):
        try:
            file_path_normalized = self.normalize_path(file_path)
            self.table.update(dict(file_path=file_path_normalized, rating=rating), ['file_path'])
            # Update cache in-place instead of full invalidation
            cached = self._entry_cache.get(file_path_normalized)
            if cached is not None:
                cached['rating'] = rating
            logging.info(f"Rating {rating} saved for {file_path_normalized}")
        except Exception as e:
            logging.error(f"Failed to save rating for {file_path}: {e}")



    def normalize_path(self, path: str) -> str:
        """Normalize path for consistent dict and DB lookups.
        - abspath: resolves relative paths and symlinks
        - normcase: lowercases on Windows (handles case-insensitive FS)
        - NFC: canonical Unicode form (e.g. é vs e + combining accent)
        """
        return unicodedata.normalize('NFC', os.path.normcase(os.path.abspath(path.strip())))

    def _guess_media_dimensions(self, file_path: str) -> tuple[int, int]:
        """Best-effort width/height for DB rows created lazily during autotag."""
        ext = os.path.splitext(file_path)[1].lower()
        if ext in VIDEO_FORMATS:
            return 0, 0
        if ext in IMAGE_FORMATS:
            try:
                from PIL import Image

                with Image.open(file_path) as img:
                    w, h = img.size
                    return int(w), int(h)
            except Exception:
                return 0, 0
        return 0, 0

    def update_folder_path(self, old_path, new_path):
        try:
            # Normalize both paths
            old_path_normalized = self.normalize_path(old_path)
            new_path_normalized = self.normalize_path(new_path)

            # 1. Update the exact item (file or folder itself)
            query_exact = "UPDATE files SET file_path = :new_path WHERE file_path = :old_path"
            self.db.query(query_exact, new_path=new_path_normalized, old_path=old_path_normalized)

            # 2. Update all children (if old_path was a folder) using string replacement
            old_prefix = old_path_normalized + os.sep
            new_prefix = new_path_normalized + os.sep
            
            # Prevent database desync and UI starvation by keeping children paths updated
            query_children = """
                UPDATE files 
                SET file_path = :new_prefix || SUBSTR(file_path, LENGTH(:old_prefix) + 1)
                WHERE file_path LIKE :old_prefix_wildcard
            """
            self.db.query(query_children, new_prefix=new_prefix, old_prefix=old_prefix, old_prefix_wildcard=old_prefix + '%')

            # 3. Clear memory cache to prevent serving stale/None data to the UI
            self.clear_entry_cache()
            
            logging.info(f"SUCCESS: Updated path and children from {old_path_normalized} to {new_path_normalized}")

        except Exception as e:
            logging.error(f"ERROR updating folder path: {e}")



    def update_keywords(self, file_path, keywords):
        """
        Update the database with keywords for the given file path.
        """
        try:
            normalized_path = self.normalize_path(file_path)
            keyword_list = [kw.strip() for kw in keywords.split(",") if kw.strip()]

            if not keyword_list and keywords.strip() == "":
                record = self._get_cached_entry(normalized_path)
                if record:
                    record["keywords"] = ""
                    self.table.update(record, ["id"])
                    # cache already holds the same dict reference, so it's updated
                return

            record = self._get_cached_entry(normalized_path)
            if record:
                existing_raw = record.get("keywords", "")
                existing_keywords = [kw.strip() for kw in existing_raw.split(",")] if existing_raw else []
                updated_keywords = sorted(set(existing_keywords + keyword_list))
                record["keywords"] = ", ".join(updated_keywords)
                self.table.update(record, ["id"])
                logging.info(f"Updated keywords for {file_path}: {record['keywords']}")
            else:
                # Autotag can run before the thumbnail pipeline inserted a row.
                # Upsert a minimal record so keywords persist and UI can refresh.
                width, height = self._guess_media_dimensions(file_path)
                filename_normalized = os.path.basename(file_path).strip().lower()
                merged = ", ".join(sorted(set(keyword_list)))
                new_row = dict(
                    filename=filename_normalized,
                    file_path=normalized_path,
                    width=width,
                    height=height,
                    keywords=merged,
                    rating=0,
                    is_cached=False,
                )
                self.table.upsert(new_row, keys=["file_path"])
                self._entry_cache[normalized_path] = self.table.find_one(file_path=normalized_path)
                logging.info(f"Inserted keywords for new DB entry {file_path}: {merged}")

        except Exception as e:
            logging.error(f"Error updating keywords for {file_path}: {e}")




    def remove_duplicates_from_db(db_path="catalog.db"):
        """
        Normalize file paths and remove duplicates from the database.
        """
        # Helper function to normalize paths
        def normalize_path(path):
            return os.path.normpath(path).lower()

        # Connect to the database
        db = dataset.connect(f"sqlite:///{db_path}", engine_kwargs=_SQLITE_ENGINE_KWARGS)
        table = db["files"]

        try:
            # Step 1: Normalize all paths in the database
            logging.info("Normalizing file paths...")
            all_entries = table.all()
            for entry in all_entries:
                old_path = entry["file_path"]
                normalized_path = normalize_path(old_path)

                # Update the path only if it has changed
                if old_path != normalized_path:
                    logging.info(f"Updating path: {old_path} -> {normalized_path}")
                    table.update({"id": entry["id"], "file_path": normalized_path}, ["id"])

            # Step 2: Identify and remove duplicates
            logging.info("Checking for duplicates...")
            duplicates_query = """
            SELECT filename, LOWER(file_path) as file_path, COUNT(*) as count
            FROM files
            GROUP BY filename, file_path
            HAVING count > 1
            """
            duplicates = db.query(duplicates_query)

            # Iterate over duplicates and remove excess entries
            for row in duplicates:
                # Fetch all rows with the same `filename` and `file_path`
                entries = list(table.find(filename=row["filename"], file_path=row["file_path"]))

                # Sort entries by ID and keep the first one, delete the rest
                entries_sorted = sorted(entries, key=lambda x: x["id"])
                for entry in entries_sorted[1:]:
                    logging.info(f"Deleting duplicate entry: ID {entry['id']}, Path {entry['file_path']}")
                    table.delete(id=entry["id"])

            logging.info("Normalization and duplicate removal complete. Only unique entries remain.")

        except Exception as e:
            logging.info(f"Error while removing duplicates or normalizing paths: {e}")

        finally:
            db.executable.close()



    def remove_entry(self, file_path):
        try:
            file_path = self.normalize_path(file_path)
            self.db.begin()
            self.table.delete(file_path=file_path)
            self.db.commit()
            self._invalidate_cache(file_path)
            self._cached_paths_set.discard(file_path)
        except Exception as e:
            logging.error(f"Error removing entry for {file_path}: {e}")
            self.db.rollback()

   
    
    def is_folder_cached(self, folder_path):
            """Quick check from memory set (for Treeview)."""
            if not folder_path:
                return False
            # We use the same normalization as when storing
            norm_path = self.normalize_path(folder_path)
            # print(f"DEBUG: Checking {norm_path} in cache...") # <-- Add this
            return norm_path in self._cached_paths_set

        



    def folder_has_cached_descendant(self, folder_path: str) -> bool:
        """
        True if folder_path or any path under it has is_cached in the fast tree set.
        Used after DnD moves to fix parent folder icons.
        """
        if not folder_path:
            return False
        norm = self.normalize_path(folder_path)
        prefix = norm + os.sep
        if norm in self._cached_paths_set:
            return True
        for p in self._cached_paths_set:
            if p.startswith(prefix):
                return True
        return False

    def update_cache_status(self, file_path, status):
        """Updates DB and memory set at once. Unified on 'file_path'."""
        norm_path = self.normalize_path(file_path)
        self.table.upsert(dict(file_path=norm_path, is_cached=status), ['file_path'])
        # Update row cache in-place
        cached = self._entry_cache.get(norm_path)
        if cached is not None:
            cached['is_cached'] = status
        # Update the fast folder-cached set
        if status:
            self._cached_paths_set.add(norm_path)
        else:
            self._cached_paths_set.discard(norm_path)



    def update_cache_statusOld(self, folder_path, is_cached):
        """
        Legacy version of update_cache_status. Uses folder_path and is_cached directly.
        Kept for backward compatibility.
        """
        norm_path = os.path.normcase(os.path.normpath(folder_path))
        existing_entry = self.table.find_one(file_path=norm_path)
        if existing_entry:
            self.table.update(dict(id=existing_entry['id'], is_cached=is_cached), ['id'])
        else:
            self.table.insert(dict(file_path=norm_path, is_cached=is_cached))
        
        
    def get_cache_status(self, file_path):
      """Alias for quick check in loops."""
      return self.is_folder_cached(file_path)
        
     
    def get_all_entries(self):
        # Get all entries from the dataset table
        return list(self.table.all())

    def get_folder_descendant_media_stats(
        self,
        folder_path: str,
        video_extensions: tuple[str, ...],
        image_extensions: tuple[str, ...],
        *,
        max_keywords: int = 12,
    ) -> dict:
        """
        Aggregate catalog rows for all files under folder_path (recursive), using DB only.

        Returns:
            video_count, image_count, ratings (sorted unique ints > 0),
            keywords (comma-separated top by frequency), extra_keyword_count (tags not shown).
        """
        norm = self.normalize_path(folder_path)
        prefix = norm + os.sep
        video_ext = {e.lower() for e in video_extensions}
        image_ext = {e.lower() for e in image_extensions}

        video_count = 0
        image_count = 0
        ratings_positive = set()
        kw_counter: Counter[str] = Counter()

        try:
            q = "SELECT file_path, rating, keywords FROM files WHERE file_path LIKE :p"
            rows = self.db.query(q, p=prefix + "%")
            for row in rows:
                fp = row.get("file_path") or ""
                _, ext = os.path.splitext(fp.lower())
                if ext in video_ext:
                    video_count += 1
                elif ext in image_ext:
                    image_count += 1

                r = row.get("rating")
                try:
                    ri = int(r) if r is not None else 0
                except (TypeError, ValueError):
                    ri = 0
                if ri > 0:
                    ratings_positive.add(ri)

                raw_kw = row.get("keywords") or ""
                if isinstance(raw_kw, str) and raw_kw.strip():
                    for part in raw_kw.split(","):
                        t = part.strip().lower()
                        if t:
                            kw_counter[t] += 1
        except Exception as e:
            logging.error(f"get_folder_descendant_media_stats failed for {folder_path!r}: {e}")
            return {
                "video_count": 0,
                "image_count": 0,
                "ratings": [],
                "keywords": "",
                "extra_keyword_count": 0,
            }

        top_pairs = kw_counter.most_common(max_keywords)
        keywords_out = ", ".join(w for w, _ in top_pairs)
        shown = {w for w, _ in top_pairs}
        extra_keyword_count = max(0, len(kw_counter) - len(shown))

        return {
            "video_count": video_count,
            "image_count": image_count,
            "ratings": sorted(ratings_positive),
            "keywords": keywords_out,
            "extra_keyword_count": extra_keyword_count,
        }

    def get_valid_columns(self):
        # Query the database to get valid columns
        valid_columns = self.db.query("PRAGMA table_info(files)")
        return [col['name'] for col in valid_columns]
    
    def search_entries(self, search_param, keyword, and_or=None, operator=None):
        try:
            logging.debug(
                "search_entries: param=%s keyword=%s operator=%s and_or=%s",
                search_param,
                keyword,
                operator,
                and_or,
            )

            # Handle numeric comparisons
            if operator in ('<=', '>=', '<', '>') and search_param in ['rating', 'width', 'height']:
                try:
                    value = float(keyword)  # Ensure the keyword is numeric
                except ValueError:
                    logging.info(f"[Error] Invalid numeric value for {search_param}: {keyword}")
                    return []

                # Build and execute numeric comparison query
                query = f"SELECT * FROM files WHERE {search_param} {operator} :value AND {search_param} > 0"
                params = {'value': value}
                logging.debug("search_entries query: %s params=%s", query, params)
                return self.db.query(query, **params)

            # Handle text searches (AND/OR logic)
            keywords = [kw.strip() for kw in keyword.split()]
            valid_columns = self.get_valid_columns()

            # Validate the search columns
            search_columns = (
                valid_columns if search_param == "all_fields" 
                else [search_param] if search_param in valid_columns 
                else []
            )
            if not search_columns:
                logging.info(f"[Error] Invalid search field: {search_param}")
                return []

            # Build the query for text searches
            query_parts = []
            params = {}
            for i, kw in enumerate(keywords):
                subquery = " OR ".join([f"{col} LIKE :term_{i}" for col in search_columns])
                query_parts.append(f"({subquery})")
                params[f"term_{i}"] = f"%{kw}%"

            query = f"SELECT * FROM files WHERE {' AND '.join(query_parts) if and_or == 'AND' else ' OR '.join(query_parts)}"
            logging.debug("search_entries query: %s params=%s", query, params)
            return self.db.query(query, **params)

        except Exception as e:
            logging.info(f"[Error] search_entries failed: {e}")
            return []


        
 