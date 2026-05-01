"""
CLIP / YOLO / ViT tagging pipeline for Vibe Player.

Loads vision models (OpenCLIP ViT-H, Hugging Face CLIP, Ultralytics YOLO), builds
text embeddings from tag files under ``tag_engine/``, and assigns labels to
images via multi-pass tiling, voting, and optional class-hint resolution.

Public API: :func:`run_tagging_pipeline` (single file), :func:`generate_tags_for_folder`
(batch). Presets and thresholds come from :class:`~app_settings.TaggingSettings`.
"""

from __future__ import annotations

import json
import logging
import os
import ssl
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from PIL import Image
from tqdm import tqdm

from app_settings import AppSettings, TaggingSettings

yolo_model = None


def get_device() -> str:
    try:
        import torch
        if not torch.cuda.is_available():
            return "cpu"
        
        device_count = torch.cuda.device_count()
        device_index = device_count - 1
        return f"cuda:{device_index}"
    except Exception:
        return "cpu"


device = get_device()

torch = None
autocast = None

BASE_DIR = Path(__file__).resolve().parent
TAGS_DIR = BASE_DIR / "tag_engine"

logging.debug("Loaded generate_tags_ilektra from: %s", __file__)

SAVE_TO_IMAGE_METADATA = False
NORMALIZE_MAIN_SUB = False
FOLDER_PATH = TAGS_DIR / "test_images"
OUTPUT_JSON = TAGS_DIR / "tags.json"


EXTEND_DETAIL_TAGS_WITH_GLOBAL = True

vit_model = None
vit_preprocess = None
global_feats = None
detail_feats = None

vit_model_cache = None
_cached_yolo_model = None
_cached_clip_model = None
_cached_clip_processor = None
_cached_tag_data: dict[str, Any] = {}
_cached_extra_models: dict[str, Any] = {}


def configure_ssl_certificates() -> None:
    """Make HTTPS model downloads robust in bundled EXE and normal Python runs."""
    try:
        import certifi
    except ImportError:
        logging.debug("certifi not installed; using system certificate store")
        return

    cafile = certifi.where()
    os.environ.setdefault("SSL_CERT_FILE", cafile)
    os.environ.setdefault("REQUESTS_CA_BUNDLE", cafile)
    try:
        ssl._create_default_https_context = lambda: ssl.create_default_context(cafile=cafile)
    except Exception as exc:
        logging.debug("Could not override default SSL context: %s", exc)


configure_ssl_certificates()


def load_vit_extra_model(settings: TaggingSettings) -> tuple[Any, Any]:
    """Load and cache the local ViT-H OpenCLIP weights and preprocess transform."""
    import open_clip
    import torch as _torch

    global vit_model_cache
    logging.debug("Entered load_vit_extra_model()")

    if vit_model_cache is not None:
        logging.debug("Returning cached ViT model")
        return vit_model_cache

    logging.info("Loading ViT-H extra fine-tuned OpenCLIP model (local)")

    model_name = "ViT-H-14"
    model_file = settings.openclip_model_path

    logging.debug("model_file: %s", model_file)

    model = open_clip.create_model(model_name, pretrained=None)
    logging.debug("model after create_model: %s", model)

    state_dict = _torch.load(model_file, map_location=device)
    logging.debug("state_dict loaded: %s", type(state_dict))

    if model is None:
        logging.error("model is None after create_model")
    if state_dict is None:
        logging.error("state_dict is None after torch.load")

    model.load_state_dict(state_dict)
    model.eval()

    _, preprocess, _ = open_clip.create_model_and_transforms(
        model_name, pretrained="laion2b_s32b_b79k"
    )

    vit_model_cache = (model, preprocess)
    return vit_model_cache


TAGGING_PRESET = "F_SUPER_AGGRESSIVE"

PRESET_CONFIGS = {
    "F_SUPER_AGGRESSIVE": {
            "CONFIDENCE_THRESHOLD": 0.03,  # Low so fallback pass stays aggressive
            "MIN_VOTES": 1,
            "PASS_CONFIDENCE_THRESHOLDS": {
                1: 0.03,  # Full-image pass
                2: 0.03,  # 2x2 tiles (lowered from 0.08 for detail coverage)
                3: 0.03,  # 3x3 tiles (lowered from 0.08 for detail coverage)
            },
            "PASS_PRIORITY": {1: 5, 2: 2, 3: 2},
            "HUMAN_VOTE_MULTIPLIER": 5
        },
    "G_HUMAN_FOCUSED": {
        "CONFIDENCE_THRESHOLD": 0.10,
        "MIN_VOTES": 1,
        "PASS_CONFIDENCE_THRESHOLDS": {1: 0.04, 2: 0.10, 3: 0.10},
        "PASS_PRIORITY": {1: 5, 2: 2, 3: 2},
        "HUMAN_VOTE_MULTIPLIER": 7
        # "USE_YOLO": False
    },
    "H_ULTRA_SAFE": {
        "CONFIDENCE_THRESHOLD": 0.12,
        "MIN_VOTES": 2,
        "PASS_CONFIDENCE_THRESHOLDS": {1: 0.06, 2: 0.15, 3: 0.15},
        "PASS_PRIORITY": {1: 4, 2: 1, 3: 1},
        "HUMAN_VOTE_MULTIPLIER": 4
        # "USE_YOLO": False
    }
}


def apply_preset_settings(settings: TaggingSettings) -> None:
    """Apply preset thresholds and resolve default model paths under ``app/models/``."""
    config = PRESET_CONFIGS.get(settings.tagging_preset)
    if config:
        if not hasattr(settings, "confidence_threshold") or settings.confidence_threshold is None:
            settings.confidence_threshold = config["CONFIDENCE_THRESHOLD"]

        settings.min_votes = config["MIN_VOTES"]
        settings.pass_confidence_thresholds = config["PASS_CONFIDENCE_THRESHOLDS"]
        settings.pass_priority = config["PASS_PRIORITY"]
        settings.human_vote_multiplier = config["HUMAN_VOTE_MULTIPLIER"]
        settings.enable_fallback = config.get("ENABLE_FALLBACK", False)

        logging.debug("Raw yolo_model_path: %s", getattr(settings, "yolo_model_path", None))
        if not hasattr(settings, "yolo_model_path") or not os.path.isabs(settings.yolo_model_path):
            settings.yolo_model_path = str((BASE_DIR / "models" / "yolov8" / "yolov8n.pt").resolve())
            logging.debug("Resolved yolo_model_path: %s", settings.yolo_model_path)
        else:
            logging.debug("yolo_model_path already absolute: %s", settings.yolo_model_path)

        # If local weights are not bundled, let Ultralytics fetch from its cache/source.
        if not os.path.exists(settings.yolo_model_path):
            logging.info(
                "Local YOLO model not found at %s; falling back to automatic download/cache key yolov8n.pt",
                settings.yolo_model_path,
            )
            settings.yolo_model_path = "yolov8n.pt"

        if not hasattr(settings, "yolo_confidence_threshold"):
            settings.yolo_confidence_threshold = 0.25
        if not hasattr(settings, "yolo_image_size"):
            settings.yolo_image_size = 640

        if not hasattr(settings, "openclip_model_dir"):
            settings.openclip_model_dir = str((BASE_DIR / "models" / "extra_engine").resolve())

        if not hasattr(settings, "openclip_model_path"):
            settings.openclip_model_path = str(
                (Path(settings.openclip_model_dir) / "open_clip_pytorch_model.bin").resolve()
            )

        logging.debug("openclip_model_dir: %s", settings.openclip_model_dir)
        logging.debug("openclip_model_path: %s", settings.openclip_model_path)

        settings.class_hint_sets = load_class_hint_sets(TAGS_DIR)

        logging.info("Applied preset: %s", settings.tagging_preset)


def load_candidate_tags(filenames: list[str | Path]) -> list[str]:
    """Load candidate tags from multiple UTF-8 text files (one tag per line)."""
    tags: set[str] = set()
    for filepath in filenames:
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                tags.update(line.strip() for line in f if line.strip())
        except FileNotFoundError:
            logging.warning("Tag file not found: %s", filepath)
    return list(tags)


def predict_yolo(image: Any, settings: TaggingSettings) -> list[str]:
    """Run the cached YOLO model on a PIL image and return class name labels."""
    results = yolo_model.predict(
        image,
        imgsz=settings.yolo_image_size,
        conf=settings.yolo_confidence_threshold,
        device=device,
        verbose=False,
    )

    detected_tags: list[str] = []
    for result in results:
        if hasattr(result, "boxes") and result.boxes is not None:
            for cls in result.boxes.cls:
                label = yolo_model.names[int(cls)]
                detected_tags.append(label)

    if detected_tags:
        logging.debug("YOLO detected: %s", ", ".join(detected_tags))

    return detected_tags


def predict_extra_feature_tags(image_path: str | Path, settings: TaggingSettings) -> list[tuple[str, float]]:
    """Walk ``app/models/extra`` for ``.pt`` models and run each on ``image_path``."""
    global _cached_extra_models

    extra_models_root = BASE_DIR / "models" / "extra"

    if not extra_models_root.is_dir():
        logging.debug("YOLO-Extra root not found, skipping: %s", extra_models_root)
        return []

    from ultralytics import YOLO

    results_list: list[tuple[str, float]] = []
    yolo_conf = 0.15 if settings.confidence_threshold < 0.1 else 0.25

    for root, _dirs, files in os.walk(extra_models_root):
        for file in files:
            if not file.endswith(".pt"):
                continue
            model_path = os.path.join(root, file)
            model_key = file

            try:
                if model_key not in _cached_extra_models:
                    logging.info("YOLO-Extra loading model: %s", file)
                    _cached_extra_models[model_key] = YOLO(model_path)

                model = _cached_extra_models[model_key]
                results = model.predict(image_path, conf=yolo_conf, verbose=False)

                for r in results:
                    for box in r.boxes:
                        label = model.names[int(box.cls[0])]
                        score = float(box.conf[0])
                        results_list.append((label, score))
                        logging.debug("YOLO-Extra %s: %s (%.2f)", file, label, score)

            except Exception as e:
                logging.error("YOLO-Extra error for model %s: %s", file, e)

    return results_list


def predict_batch_vit_extra(
    images: list[Image.Image],
    model: Any,
    preprocess: Any,
    text_feats: Any,
    candidate_tags: list[str],
    settings: TaggingSettings,
    threshold: float | None = None,
    fallback_top_k: int = 5,
) -> list[list[str]]:
    """ViT/OpenCLIP batch: softmax over text embeddings, optional top-k fallback."""
    if threshold is None:
        threshold = settings.confidence_threshold

    logging.debug(
        "predict_batch_vit_extra: %s image(s), threshold=%s",
        len(images),
        threshold,
    )

    batch = torch.stack([preprocess(img) for img in images]).to(device)
    logging.debug(
        "batch shape=%s device=%s model_device=%s",
        batch.shape,
        batch.device,
        next(model.parameters()).device,
    )
    logging.debug(
        "text_feats device=%s dtype=%s batch dtype=%s",
        getattr(text_feats, "device", None),
        getattr(text_feats, "dtype", None),
        batch.dtype,
    )

    with torch.no_grad():
        img_feats = model.encode_image(batch)
        logging.debug("img_feats device=%s dtype=%s", img_feats.device, img_feats.dtype)
        logging.debug(
            "text_feats device=%s dtype=%s",
            text_feats.device,
            text_feats.dtype,
        )
        img_feats /= img_feats.norm(dim=-1, keepdim=True)
        text_feats = text_feats.to(img_feats.dtype).to(device)

        logits = img_feats @ text_feats.T
        probs = logits.softmax(dim=-1).cpu().tolist()

    all_tags: list[list[str]] = []
    for idx, prob_list in enumerate(probs):
        top_probs = sorted(zip(candidate_tags, prob_list), key=lambda x: x[1], reverse=True)
        tags = [tag for tag, p in top_probs if p >= threshold]

        if not tags and settings.enable_fallback:
            tags = [tag for tag, _ in top_probs[:fallback_top_k]]
            logging.debug("VIT fallback (no tags above threshold): %s", tags)

        all_tags.append(tags)

        debug_line = ", ".join(f"{tag} ({p:.3f})" for tag, p in top_probs[:10])
        logging.debug("VIT image %s top-10: %s", idx, debug_line)

    return all_tags


def predict_batch(
    images: list[Image.Image],
    candidate_tags: list[str],
    model: Any,
    processor: Any,
    threshold: float = 0.25,
) -> list[list[str]]:
    """Hugging Face CLIP: softmax over text–image logits per image."""
    inputs = processor(
        text=candidate_tags, images=images, return_tensors="pt", padding=True
    ).to(device)

    with torch.no_grad():
        outputs = model(**inputs)

    logits_per_image = outputs.logits_per_image
    probs = logits_per_image.softmax(dim=1)

    all_tags = []
    for prob in probs:
        tags = []
        for idx, score in enumerate(prob):
            if score.item() > threshold:
                tags.append(candidate_tags[idx])
        all_tags.append(tags)

    return all_tags


def save_tags_to_metadata(
    image_path: str | Path,
    tag_metadata_list: list[str],
    output_path: str,
) -> None:
    """Write tag lines into JPEG EXIF ImageDescription (tag 270)."""
    try:
        image = Image.open(image_path).convert("RGB")

        metadata_string = "\n".join(tag_metadata_list)

        exif = image.getexif()
        exif[270] = metadata_string  # ImageDescription

        if not output_path.lower().endswith(".jpg"):
            output_path = output_path.rsplit(".", 1)[0] + ".jpg"

        image.save(output_path, "JPEG", exif=exif)

        logging.info("Saved tag metadata to %s", output_path)

    except Exception as e:
        logging.warning("Could not save tag metadata: %s", e)


def precompute_splits(image: Image.Image, max_passes: int) -> dict[int, list[Image.Image]]:
    """Build ``pass_number -> list of PIL crops`` for 1x1 … NxN grid tiling."""
    width, height = image.size
    splits = {}
    for pass_number in range(1, max_passes + 1):
        tiles = []
        tile_width = width // pass_number
        tile_height = height // pass_number
        for i in range(pass_number):
            for j in range(pass_number):
                left = j * tile_width
                upper = i * tile_height
                right = (j + 1) * tile_width
                lower = (i + 1) * tile_height
                tiles.append(image.crop((left, upper, right, lower)))
        splits[pass_number] = tiles
    return splits


def load_class_hint_sets(directory: str | Path) -> dict[str, list[str]]:
    """Load ``# class`` blocks from ``*hint*.txt`` files under ``directory``."""
    class_hint_sets: dict[str, list[str]] = {}

    if not os.path.exists(directory):
        logging.warning("Class-hint directory not found: %s", directory)
        return class_hint_sets

    for file in os.listdir(directory):
        if file.endswith(".txt") and "hint" in file.lower():
            filepath = os.path.join(directory, file)
            current_class = None
            try:
                with open(filepath, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        if line.startswith("#"):
                            current_class = line[1:].strip()
                            # Ensure we don't overwrite if multiple files define the same class
                            if current_class not in class_hint_sets:
                                class_hint_sets[current_class] = []
                        elif current_class:
                            class_hint_sets[current_class].append(line)
            except Exception as e:
                logging.error("Error reading class hints %s: %s", filepath, e)

    return class_hint_sets


def vote_tags(
    all_detected_tags: list[tuple[str, int]],
    pass_priority: dict[int, int],
    human_tags: list[str],
    min_votes: int,
    human_vote_multiplier: int,
) -> tuple[list[str], list[str]]:
    """Aggregate weighted votes across passes; return final tags and detail strings."""
    from collections import defaultdict

    weighted_votes: defaultdict[str, int] = defaultdict(int)
    for tag, pass_num in all_detected_tags:
        weight = pass_priority.get(pass_num, 1)
        if tag.lower() in human_tags:
            weight *= human_vote_multiplier
        weighted_votes[tag] += weight

    final_tags = []
    vote_details = []
    for tag, count in sorted(weighted_votes.items(), key=lambda x: x[1], reverse=True):
        if count >= min_votes:
            final_tags.append(tag)
            vote_details.append(f"[{count}×] {tag}")
    return final_tags, vote_details


def smart_gender_cleanup(tags: list[str]) -> list[str]:
    """Drop unreliable feminine cues when male facial-hair cues are present."""
    male_indicators = {"beard", "stubble", "facial hair", "mustache", "goatee"}
    unreliable_tags = {"eyelashes", "groomed brows", "soft gaze", "smooth skin"}

    tags_set = set(tags)
    exclude = set(unreliable_tags)
    if male_indicators & tags_set:
        exclude |= {"female", "woman", "girl", "lady"}

    return [t for t in tags if t not in exclude]


def guess_class_from_description(
    tags: list[str],
    class_hint_sets: dict[str, list[str]],
    debug: bool = False,
) -> tuple[list[str], list[str], str | None]:
    """
    Promote a class when enough hint phrases match; optionally log matches at DEBUG.

    Returns:
        (tags, hint_log_lines, best_class_or_none).
    """
    tags_lower = [tag.lower() for tag in tags]
    class_scores = {}
    class_hint_detections = []

    for class_name, hints in class_hint_sets.items():
        score = 0
        for hint in hints:
            if hint.lower() in tags_lower:
                score += 1
                if debug:
                    logging.debug("Class hint match: %r -> %r", hint, class_name)
                class_hint_detections.append(f"[ClassHint] {hint} → {class_name}")
        class_scores[class_name] = score

    if not class_scores:
        return tags, class_hint_detections, None

    best_class = max(class_scores, key=class_scores.get)
    best_score = class_scores[best_class]

    if best_score >= 2:
        if debug:
            logging.debug("Class hint winner: %s (score=%s)", best_class, best_score)
        class_hint_detections.append(f"[ClassResult] {best_class} (score={best_score})")
        for other_class in class_scores:
            if other_class != best_class and other_class in tags:
                tags.remove(other_class)
        if best_class not in tags:
            tags.append(best_class)

    return tags, class_hint_detections, best_class


def infer_human(tags: list[str], human_tags: list[str], _global_tags: list[str]) -> list[str]:
    """Append ``human`` when human-vocabulary tags appear without person/human."""
    lower_tags = {tag.lower() for tag in tags}
    human_set = {ht.lower() for ht in human_tags}

    if lower_tags & human_set and "human" not in tags and "person" not in tags:
        tags.append("human")
    return tags


def load_image(path: str | Path) -> Image.Image | None:
    try:
        return Image.open(path).convert("RGB")
    except Exception as e:
        logging.warning("Failed to load image %s: %s", path, e)
        return None


def process_image(
    path: str | Path,
    global_tags: list[str],
    detail_tags: list[str],
    human_tags: list[str],
    model: Any,
    processor: Any,
    global_feats: Any,
    detail_feats: Any,
    settings: TaggingSettings,
) -> tuple[str, list[str], str | None]:
    """Multi-pass CLIP/ViT tiling, optional YOLO, voting, class hints, and gender cleanup."""
    path_str = os.fspath(path)
    engine = getattr(settings, "tagging_engine", "CLIP").upper()
    logging.info("AutoTag: %s", os.path.basename(path_str))
    logging.debug("Engine=%s preset=%s", engine, settings.tagging_preset)

    ensure_torch_imported()
    fname = os.path.basename(path_str)
    img = load_image(path)

    all_votes: list[tuple[str, int]] = []

    if engine != "YOLO":
        splits = precompute_splits(img, max_passes=3)
        for p, flat_tiles in splits.items():
            pass_found: list[str] = []
            threshold = settings.pass_confidence_thresholds.get(p, settings.confidence_threshold)

            logging.debug("Pass %s: %s tiles, threshold=%s", p, len(flat_tiles), threshold)

            if engine == "VIT":
                batch = torch.stack([processor(t) for t in flat_tiles]).to(device)
                if str(device).startswith("cuda"):
                    batch = batch.half()

                with torch.no_grad(), autocast("cuda" if torch.cuda.is_available() else "cpu"):
                    img_feats = model.encode_image(batch)
                    img_feats /= img_feats.norm(dim=-1, keepdim=True)

                detail_feats = detail_feats.to(img_feats.device).to(img_feats.dtype)
                logits = img_feats @ detail_feats.T
                probs = logits.softmax(dim=-1).cpu().tolist()
            else:
                clip_inputs = processor(images=flat_tiles, return_tensors="pt", padding=True).to(device)
                with torch.no_grad(), autocast("cuda" if torch.cuda.is_available() else "cpu"):
                    img_feats = model.get_image_features(**clip_inputs)
                    if hasattr(img_feats, "pooler_output"):
                        img_feats = img_feats.pooler_output
                    img_feats /= img_feats.norm(dim=-1, keepdim=True)

                detail_feats = detail_feats.to(img_feats.device).to(img_feats.dtype)
                logits = img_feats @ detail_feats.T
                probs = logits.softmax(dim=-1).cpu().tolist()

            for prob_list in probs:
                for tag, p_score in zip(detail_tags, prob_list):
                    if p_score >= threshold:
                        all_votes.append((tag, p))
                        pass_found.append(f"{tag}({p_score:.2f})")

            if pass_found:
                unique_finds = sorted(set(pass_found))
                logging.debug("Pass %s tags: %s", p, ", ".join(unique_finds))
            else:
                logging.debug("Pass %s: no tags above threshold", p)

    if engine == "YOLO":
        yolo_tags = predict_yolo(img, settings)
        if yolo_tags:
            logging.debug("YOLO tags: %s", ", ".join(yolo_tags))
            for t in yolo_tags:
                all_votes.append((t, 1))

    if os.path.exists(path_str):
        feature_tags = predict_extra_feature_tags(path, settings)
        if feature_tags:
            feature_log = [f"{t}({s:.2f})" for t, s in feature_tags]
            logging.debug("Extra YOLO features: %s", ", ".join(feature_log))
            for tag, score in feature_tags:
                all_votes.append((tag, 1))

    final_tags, vote_details = vote_tags(
        all_votes,
        settings.pass_priority,
        human_tags,
        settings.min_votes,
        settings.human_vote_multiplier,
    )

    if len(final_tags) < settings.min_votes:
        if engine == "YOLO":
            logging.info(
                "Fallback skipped for YOLO-only mode: insufficient votes (%s/%s)",
                len(final_tags),
                settings.min_votes,
            )
        else:
            logging.info(
                "Fallback: insufficient votes (%s/%s), using global pass",
                len(final_tags),
                settings.min_votes,
            )
        if engine == "VIT":
            fb = predict_batch_vit_extra([img], model, processor, global_feats, global_tags, settings)[0]
            final_tags = fb
            vote_details = [f"[Tag] {t}(fb)" for t in fb]
        elif engine != "YOLO":
            fb = predict_batch([img], global_tags, model, processor, threshold=settings.confidence_threshold)[0]
            final_tags = fb
            vote_details = [f"[Tag] {t}(fb)" for t in fb]

    final_tags = infer_human(final_tags, human_tags, global_tags)
    final_tags, hint_logs, main_sub = guess_class_from_description(
        final_tags, settings.class_hint_sets, debug=False
    )

    if any(t.lower() in {"human", "person"} for t in final_tags):
        final_tags = smart_gender_cleanup(final_tags)

    if SAVE_TO_IMAGE_METADATA:
        out_dir = os.path.join(os.path.dirname(path_str), "img_metadata")
        os.makedirs(out_dir, exist_ok=True)
        save_tags_to_metadata(path_str, vote_details + hint_logs, os.path.join(out_dir, f"tagged_{fname}"))

    logging.info("Tagged %s: %s", fname, ", ".join(final_tags))
    return fname, final_tags, main_sub


def ensure_torch_imported() -> None:
    global torch, autocast
    if torch is None:
        try:
            import torch as _torch
            from torch.amp import autocast as _autocast

            torch = _torch
            autocast = _autocast
            logging.debug("PyTorch imported successfully")
        except ImportError:
            from contextlib import nullcontext

            torch = None
            autocast = nullcontext
            logging.warning("PyTorch not available; tagging requires torch")


def get_or_load_models_and_tags(settings: TaggingSettings) -> tuple[Any, Any, dict[str, Any]]:
    """Load YOLO + CLIP or ViT, then load or build OpenCLIP text embedding caches."""
    global _cached_yolo_model, _cached_clip_model, _cached_clip_processor, _cached_tag_data, yolo_model

    t_total_start = time.perf_counter()
    ensure_torch_imported()

    engine = getattr(settings, "tagging_engine", "CLIP").upper()
    logging.info("Tagging init: engine=%s device=%s", engine, device)
    if torch is not None:
        try:
            logging.info("CUDA available=%s device_count=%s", torch.cuda.is_available(), torch.cuda.device_count())
            if torch.cuda.is_available():
                idx = torch.cuda.current_device()
                logging.info("CUDA device=%s name=%s", idx, torch.cuda.get_device_name(idx))
        except Exception as e:
            logging.debug("Could not read CUDA device info: %s", e)

    if _cached_yolo_model is None:
        logging.info("Loading YOLO model...")
        from ultralytics import YOLO

        t_yolo_start = time.perf_counter()
        _cached_yolo_model = YOLO(settings.yolo_model_path)
        logging.info("YOLO model loaded in %.2fs", time.perf_counter() - t_yolo_start)
    yolo_model = _cached_yolo_model

    if engine != "YOLO" and _cached_clip_model is None:
        logging.info("Loading %s vision model...", engine)
        t_clip_start = time.perf_counter()
        if engine == "VIT":
            _cached_clip_model, _cached_clip_processor = load_vit_extra_model(settings)
            _cached_clip_model.to(device).eval()
        else:
            from transformers import CLIPModel, CLIPProcessor

            _cached_clip_model = CLIPModel.from_pretrained("openai/clip-vit-base-patch32").to(device).eval()
            _cached_clip_processor = CLIPProcessor.from_pretrained("openai/clip-vit-base-patch32")
        logging.info("%s vision model loaded in %.2fs", engine, time.perf_counter() - t_clip_start)

        if str(device).startswith("cuda"):
            _cached_clip_model.half()
            logging.info("Vision model using float16 (CUDA)")

    import pickle

    cache_key = f"{engine}_{settings.tagging_preset}"
    embedding_cache_path = TAGS_DIR / f"cache_{cache_key}.pkl"

    if cache_key not in _cached_tag_data:
        if engine == "YOLO":
            g_files: list[str] = []
            h_files: list[str] = []
            if TAGS_DIR.is_dir():
                for file in os.listdir(TAGS_DIR):
                    if file.endswith(".txt"):
                        fpath = os.fspath(TAGS_DIR / file)
                        name = file.lower()
                        if "human" in name or "extra" in name:
                            h_files.append(fpath)
                            g_files.append(fpath)
                        elif "detail" not in name and "hint" not in name:
                            g_files.append(fpath)

            g_tags = load_candidate_tags(g_files)
            h_tags = load_candidate_tags(h_files)
            _cached_tag_data[cache_key] = {
                "g_tags": g_tags,
                "d_tags": [],
                "h_tags": h_tags,
                "g_feats": None,
                "d_feats": None,
            }
            logging.info(
                "YOLO-only mode: skipped CLIP embedding init (global=%d, human=%d)",
                len(g_tags),
                len(h_tags),
            )
            logging.info("Tagging init total time: %.2fs", time.perf_counter() - t_total_start)
            return _cached_clip_model, _cached_clip_processor, _cached_tag_data[cache_key]

        if embedding_cache_path.is_file():
            logging.info("Loading embedding cache: %s", embedding_cache_path)
            t_cache_load = time.perf_counter()
            with open(embedding_cache_path, "rb") as f:
                _cached_tag_data[cache_key] = pickle.load(f)
            logging.info("Embedding cache loaded in %.2fs", time.perf_counter() - t_cache_load)
        else:
            logging.info("Computing text embeddings for %s (first run may be slow)...", cache_key)
            t_embed_total = time.perf_counter()
            import open_clip

            g_files: list[str] = []
            d_files: list[str] = []
            h_files: list[str] = []
            if TAGS_DIR.is_dir():
                for file in os.listdir(TAGS_DIR):
                    if file.endswith(".txt"):
                        fpath = os.fspath(TAGS_DIR / file)
                        name = file.lower()
                        if "human" in name or "extra" in name:
                            h_files.append(fpath)
                            g_files.append(fpath)
                            d_files.append(fpath)
                        elif "detail" in name or "hint" in name:
                            d_files.append(fpath)
                        else:
                            g_files.append(fpath)

            g_tags = load_candidate_tags(g_files)
            d_tags = load_candidate_tags(d_files)
            h_tags = load_candidate_tags(h_files)
            logging.info(
                "Tag sets prepared: global=%d detail=%d human=%d",
                len(g_tags),
                len(d_tags),
                len(h_tags),
            )

            if EXTEND_DETAIL_TAGS_WITH_GLOBAL:
                d_tags = list(set(d_tags + g_tags))
                logging.info("Detail tags extended with global tags: detail=%d", len(d_tags))

            t_tok = time.perf_counter()
            tokens_g = open_clip.tokenize(g_tags).to(device)
            tokens_d = open_clip.tokenize(d_tags).to(device)
            logging.info(
                "Tokenization done in %.2fs (tokens_g=%s, tokens_d=%s)",
                time.perf_counter() - t_tok,
                tuple(tokens_g.shape),
                tuple(tokens_d.shape),
            )

            t_feats = time.perf_counter()
            with torch.no_grad(), autocast("cuda" if torch.cuda.is_available() else "cpu"):
                if engine == "VIT":
                    g_feats = _cached_clip_model.encode_text(tokens_g)
                    d_feats = _cached_clip_model.encode_text(tokens_d)
                else:
                    g_feats = _cached_clip_model.get_text_features(input_ids=tokens_g)
                    d_feats = _cached_clip_model.get_text_features(input_ids=tokens_d)

                if hasattr(g_feats, "pooler_output"):
                    g_feats = g_feats.pooler_output
                if hasattr(d_feats, "pooler_output"):
                    d_feats = d_feats.pooler_output

                g_feats /= g_feats.norm(dim=-1, keepdim=True)
                d_feats /= d_feats.norm(dim=-1, keepdim=True)
            logging.info(
                "Text feature compute done in %.2fs (g_feats=%s, d_feats=%s)",
                time.perf_counter() - t_feats,
                tuple(g_feats.shape),
                tuple(d_feats.shape),
            )
            if torch is not None:
                try:
                    if torch.cuda.is_available():
                        idx = torch.cuda.current_device()
                        allocated = torch.cuda.memory_allocated(idx) / (1024 ** 3)
                        reserved = torch.cuda.memory_reserved(idx) / (1024 ** 3)
                        logging.info("CUDA memory after embeddings: allocated=%.2fGB reserved=%.2fGB", allocated, reserved)
                except Exception as e:
                    logging.debug("Could not read CUDA memory stats: %s", e)

            _cached_tag_data[cache_key] = {
                "g_tags": g_tags,
                "d_tags": d_tags,
                "h_tags": h_tags,
                "g_feats": g_feats,
                "d_feats": d_feats,
            }
            t_cache_write = time.perf_counter()
            with open(embedding_cache_path, "wb") as f:
                pickle.dump(_cached_tag_data[cache_key], f)
            logging.info("Wrote embedding cache: %s", embedding_cache_path)
            try:
                cache_size_mb = embedding_cache_path.stat().st_size / (1024 * 1024)
                logging.info(
                    "Embedding cache write done in %.2fs (size=%.2fMB, total_embed=%.2fs)",
                    time.perf_counter() - t_cache_write,
                    cache_size_mb,
                    time.perf_counter() - t_embed_total,
                )
            except Exception as e:
                logging.debug("Could not stat embedding cache file: %s", e)

    logging.info("Tagging init total time: %.2fs", time.perf_counter() - t_total_start)
    return _cached_clip_model, _cached_clip_processor, _cached_tag_data[cache_key]


def generate_tags_for_folder(folder_path: str | Path, output_json: str | Path) -> None:
    """Batch-tag all JPEG/PNG images under ``folder_path`` and write ``output_json``."""
    folder_path = Path(folder_path)
    output_json = Path(output_json)

    logging.info("Batch tagging folder: %s", folder_path)

    settings = AppSettings.load().tagging
    apply_preset_settings(settings)

    clip_model, clip_processor, data = get_or_load_models_and_tags(settings)

    image_files = [
        os.fspath(folder_path / fn)
        for fn in os.listdir(folder_path)
        if fn.lower().endswith((".jpg", ".jpeg", ".png"))
    ]
    if not image_files:
        logging.warning("No images found in folder: %s", folder_path)
        return

    results: dict[str, dict[str, Any]] = {}

    def _worker(p: str):
        return process_image(
            p,
            data["g_tags"],
            data["d_tags"],
            data["h_tags"],
            clip_model,
            clip_processor,
            data["g_feats"],
            data["d_feats"],
            settings,
        )

    with ThreadPoolExecutor() as executor:
        futures = {executor.submit(_worker, p): p for p in image_files}
        for fut in tqdm(as_completed(futures), total=len(futures), desc="Processing images"):
            filename, tags, main_subject = fut.result()
            if not main_subject:
                main_subject = "unknown"
            if NORMALIZE_MAIN_SUB:
                main_subject = normalize_main_subject(main_subject)
            results[filename] = {"tags": tags, "main_subject": main_subject}

    with open(output_json, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=4)
    logging.info("Wrote batch tagging results: %s", output_json)


def normalize_main_subject(subject: str | None) -> str:
    """Map alternate subject labels to canonical ones (e.g. lady -> woman)."""
    subject_mapping = {
        "lady": "woman",
        "girl": "woman",
        "young woman": "woman",
        "boy": "man",
        "gentleman": "man",
        "fitness model": "fitness model",
    }
    if subject is None:
        return "unknown"
    return subject_mapping.get(subject.lower(), subject)


TAGS_JSON_PATH = TAGS_DIR / "tags.json"


def run_tagging_pipeline(file_path: str | Path, settings: TaggingSettings) -> list[str]:
    """Load models if needed, apply preset, and return tag strings for one image path."""
    logging.debug("run_tagging_pipeline: %s", file_path)
    apply_preset_settings(settings)

    clip_model, clip_processor, data = get_or_load_models_and_tags(settings)

    _, tags, _ = process_image(
        file_path,
        data["g_tags"],
        data["d_tags"],
        data["h_tags"],
        clip_model,
        clip_processor,
        data["g_feats"],
        data["d_feats"],
        settings,
    )

    return tags


if __name__ == "__main__":
    generate_tags_for_folder(FOLDER_PATH, OUTPUT_JSON)
