"""
Extract embedded ComfyUI / A1111 prompts from images and videos.

Images: PNG/WebP text chunks via PIL.
Videos: format/stream tags via ffprobe.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import threading
from dataclasses import dataclass, field
from difflib import SequenceMatcher, unified_diff
from typing import Any

_log = logging.getLogger(__name__)

# Stable palette for <picture N> / <Subject N> style reference tokens
REF_COLOR_PALETTE: tuple[str, ...] = (
    "#4ade80",  # green
    "#60a5fa",  # blue
    "#f472b6",  # pink
    "#fbbf24",  # amber
    "#a78bfa",  # violet
    "#34d399",  # emerald
    "#fb923c",  # orange
    "#22d3ee",  # cyan
    "#e879f9",  # fuchsia
    "#f87171",  # red
    "#a3e635",  # lime
    "#38bdf8",  # sky
    "#c084fc",  # purple
    "#facc15",  # yellow
    "#2dd4bf",  # teal
    "#fb7185",  # rose
)

# Angle-bracket refs: <Picture 1>, <Subject 1>, <video 2>, <Composition_Ref>, …
_REF_TOKEN_RE = re.compile(
    r"<([A-Za-z][A-Za-z0-9_\s]{0,48})>",
)

_PROMPT_META_KEYS = (
    "prompt",
    "workflow",
    "parameters",
    "Comment",
    "comment",
    "description",
    "Description",
    "UserComment",
)

_IMAGE_EXTS = {".png", ".webp"}

_cache_lock = threading.Lock()
_cache: dict[str, tuple[tuple[int, int] | None, "PromptInfo"]] = {}


@dataclass
class PromptInfo:
    positive: str = ""
    negative: str = ""
    model: str = ""
    source_key: str = ""
    found: bool = False
    error: str = ""
    detected_nodes: list[str] = field(default_factory=list)

    def display_text(self) -> str:
        """Human-readable body for the prompt preview panel."""
        if self.error and not self.found:
            return self.error
        if not self.found:
            return "No embedded prompt found in this file."

        parts: list[str] = []
        if self.model:
            parts.append(f"Model: {self.model}")
        if self.source_key:
            parts.append(f"Source: {self.source_key}")
        if parts:
            parts.append("")
        if self.positive:
            parts.append(self.positive)
        if self.negative:
            parts.append("")
            parts.append("--- Negative ---")
            parts.append(self.negative)
        return "\n".join(parts).strip() or "No embedded prompt found in this file."


def _file_fingerprint(path: str) -> tuple[int, int] | None:
    try:
        st = os.stat(path)
        mtime_ns = int(getattr(st, "st_mtime_ns", int(st.st_mtime * 1e9)))
        return (mtime_ns, int(st.st_size))
    except OSError:
        return None


def _find_ffprobe() -> str | None:
    try:
        from file_operations import get_ffprobe_path

        return get_ffprobe_path()
    except Exception:
        pass
    which = shutil.which("ffprobe")
    if which:
        return which
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for candidate in (
        os.path.join(repo_root, "tools", "ffmpeg", "bin", "ffprobe.exe"),
        os.path.join(repo_root, "tools", "ffmpeg", "ffprobe.exe"),
    ):
        if os.path.isfile(candidate):
            return candidate
    return None


def _startupinfo():
    if os.name != "nt":
        return None
    info = subprocess.STARTUPINFO()
    info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    info.wShowWindow = subprocess.SW_HIDE
    return info


def _try_json(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    if not isinstance(value, str):
        return value
    text = value.strip()
    if not text or text[0] not in "{[":
        return value
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError, ValueError):
        return value


def extract_raw_metadata(file_path: str) -> dict[str, Any]:
    """Return parsed metadata payloads keyed by tag/chunk name."""
    if not os.path.isfile(file_path):
        return {}

    ext = os.path.splitext(file_path)[1].lower()
    raw: dict[str, Any] = {}

    # Image chunks (PNG/WebP text; JPEG may expose limited info)
    if ext in _IMAGE_EXTS or ext in {".jpg", ".jpeg", ".jpe", ".jfif", ".gif"}:
        try:
            from PIL import Image

            with Image.open(file_path) as img:
                for key, val in (img.info or {}).items():
                    if isinstance(val, (bytes, bytearray)):
                        try:
                            val = val.decode("utf-8", errors="replace")
                        except Exception:
                            continue
                    raw[str(key)] = _try_json(val)
        except Exception as exc:
            _log.debug("[PromptExtract] PIL read failed for %s: %s", file_path, exc)
        return raw

    # Video (and other) tags via ffprobe
    ffprobe = _find_ffprobe()
    if not ffprobe:
        return raw

    cmd = [
        ffprobe,
        "-v",
        "quiet",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        file_path,
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            startupinfo=_startupinfo(),
            timeout=15,
        )
        if result.returncode != 0 or not (result.stdout or "").strip():
            return raw
        probe = json.loads(result.stdout)
        tags: dict[str, Any] = {}
        tags.update(probe.get("format", {}).get("tags") or {})
        for stream in probe.get("streams") or []:
            for k, v in (stream.get("tags") or {}).items():
                tags.setdefault(k, v)
        for key, val in tags.items():
            raw[str(key)] = _try_json(val)
    except Exception as exc:
        _log.debug("[PromptExtract] ffprobe failed for %s: %s", file_path, exc)

    return raw


def _normalize_nodes(graph_or_workflow: dict) -> dict[str, dict]:
    if "nodes" in graph_or_workflow and isinstance(graph_or_workflow["nodes"], list):
        out: dict[str, dict] = {}
        for idx, node in enumerate(graph_or_workflow["nodes"]):
            if isinstance(node, dict):
                out[str(node.get("id", idx))] = node
        return out
    return {
        str(k): v
        for k, v in graph_or_workflow.items()
        if isinstance(v, dict) and ("class_type" in v or "type" in v or "inputs" in v)
    }


def _resolve_linked_text(nodes: dict[str, dict], link: Any) -> str:
    """Follow Comfy link ``[node_id, slot]`` to a string input."""
    if not (isinstance(link, list) and len(link) >= 1):
        return ""
    ref = nodes.get(str(link[0]))
    if not isinstance(ref, dict):
        return ""
    inputs = ref.get("inputs") or {}
    if isinstance(inputs, dict):
        for key in ("text", "string", "value", "prompt", "prompt_text"):
            val = inputs.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
    widgets = ref.get("widgets_values")
    if isinstance(widgets, list):
        for item in widgets:
            if isinstance(item, str) and len(item.strip()) > 5:
                return item.strip()
    return ""


def parse_prompt_from_graph(graph_or_workflow: dict) -> PromptInfo:
    """Walk execution graph / workflow UI JSON for positive/negative prompts."""
    info = PromptInfo()
    if not isinstance(graph_or_workflow, dict):
        return info

    nodes = _normalize_nodes(graph_or_workflow)
    clip_texts: list[tuple[str, str]] = []  # (node_id, text)
    heuristic: list[str] = []

    for node_id, node in nodes.items():
        class_type = str(node.get("class_type") or node.get("type") or "")
        inputs = node.get("inputs")
        if inputs is None:
            inputs = node.get("widgets_values") or {}

        if class_type:
            info.detected_nodes.append(f"{class_type} (ID: {node_id})")

        ctype_l = class_type.lower()

        # Checkpoint / model name
        if "checkpoint" in ctype_l or class_type in ("UNETLoader", "CheckpointLoaderSimple"):
            if isinstance(inputs, dict):
                for key in ("ckpt_name", "unet_name", "model_name"):
                    val = inputs.get(key)
                    if isinstance(val, str) and val.strip() and not info.model:
                        info.model = val.strip()

        # Minimax / video generator nodes — often a single prompt field
        if "minimax" in ctype_l or "wan" in ctype_l or "kling" in ctype_l:
            if not info.model:
                info.model = class_type
            if isinstance(inputs, dict):
                for key in ("prompt", "prompt_text", "positive", "text", "description"):
                    val = inputs.get(key)
                    if isinstance(val, str) and val.strip():
                        info.positive = val.strip()
                        info.found = True
                        return info
                    if isinstance(val, list):
                        linked = _resolve_linked_text(nodes, val)
                        if linked:
                            info.positive = linked
                            info.found = True
                            return info

        if class_type in (
            "CLIPTextEncode",
            "CLIPTextEncodeSDXL",
            "CR Prompt Text",
            "TextMultitrack",
            "CLIPTextEncodeFlux",
        ):
            text = ""
            if isinstance(inputs, dict):
                val = inputs.get("text")
                if isinstance(val, str) and val.strip():
                    text = val.strip()
                elif isinstance(val, list):
                    text = _resolve_linked_text(nodes, val)
            elif isinstance(inputs, list):
                for item in inputs:
                    if isinstance(item, str) and len(item.strip()) > 5:
                        text = item.strip()
                        break
            if text:
                clip_texts.append((node_id, text))

        if isinstance(inputs, list):
            for item in inputs:
                if isinstance(item, str) and len(item.strip()) > 10:
                    heuristic.append(item.strip())
        if isinstance(inputs, dict):
            for key, val in inputs.items():
                if not isinstance(val, str) or len(val.strip()) < 5:
                    continue
                kl = key.lower()
                if any(x in kl for x in ("prompt", "positive", "caption", "text")):
                    heuristic.append(val.strip())

    if clip_texts:
        # Heuristic: longest = positive; second longest often negative
        ordered = sorted(clip_texts, key=lambda t: len(t[1]), reverse=True)
        info.positive = ordered[0][1]
        if len(ordered) > 1 and ordered[1][1] != info.positive:
            # Prefer shorter as negative when clearly smaller
            if len(ordered[1][1]) < len(info.positive) or len(ordered) == 2:
                info.negative = ordered[1][1]
        info.found = True
        return info

    if heuristic:
        info.positive = max(heuristic, key=len)
        info.found = True

    return info


def _parse_a1111_parameters(text: str) -> PromptInfo:
    """Parse Automatic1111-style ``parameters`` PNG chunk."""
    info = PromptInfo(source_key="parameters")
    if not isinstance(text, str) or not text.strip():
        return info
    body = text.replace("\r\n", "\n").replace("\r", "\n")
    neg_idx = body.find("\nNegative prompt:")
    steps_idx = body.find("\nSteps:")
    if neg_idx >= 0:
        info.positive = body[:neg_idx].strip()
        rest = body[neg_idx + len("\nNegative prompt:") :]
        if steps_idx > neg_idx:
            info.negative = body[neg_idx + len("\nNegative prompt:") : steps_idx].strip()
        else:
            # Negative until end or until Steps on same block
            cut = rest.find("\nSteps:")
            info.negative = (rest[:cut] if cut >= 0 else rest).strip()
    elif steps_idx >= 0:
        info.positive = body[:steps_idx].strip()
    else:
        info.positive = body.strip()
    # Model from Model: or Model hash
    m = re.search(r"\bModel:\s*([^,\n]+)", body)
    if m:
        info.model = m.group(1).strip()
    info.found = bool(info.positive)
    return info


def parse_prompt_from_metadata(raw: dict[str, Any]) -> PromptInfo:
    """Pick the best prompt payload from a raw metadata dict."""
    if not raw:
        return PromptInfo(error="No metadata tags found.")

    # A1111 parameters (plain string)
    for key in ("parameters", "Parameters"):
        if key in raw and isinstance(raw[key], str):
            parsed = _parse_a1111_parameters(raw[key])
            if parsed.found:
                return parsed

    best = PromptInfo()
    for key in _PROMPT_META_KEYS:
        if key not in raw:
            continue
        payload = _try_json(raw[key])
        if isinstance(payload, str):
            # Sometimes workflow is double-encoded string
            payload = _try_json(payload)
        if isinstance(payload, dict):
            candidate = parse_prompt_from_graph(payload)
            candidate.source_key = key
            if candidate.found and (
                not best.found or len(candidate.positive) > len(best.positive)
            ):
                best = candidate
            elif not best.detected_nodes and candidate.detected_nodes:
                best = candidate
                best.source_key = key

    if not best.found:
        # Last resort: any string tag that looks like a long prompt
        for key, val in raw.items():
            if isinstance(val, str) and len(val.strip()) > 40 and "\n" not in val[:20]:
                if any(x in key.lower() for x in ("prompt", "comment", "description")):
                    best = PromptInfo(
                        positive=val.strip(),
                        source_key=key,
                        found=True,
                    )
                    break

    if not best.found:
        best.error = "No embedded prompt found in this file."
    return best


def extract_prompt(file_path: str | os.PathLike[str] | None, *, use_cache: bool = True) -> PromptInfo:
    """
    Extract prompt info for an image/video path.

    Results are cached by ``(path, mtime_ns, size)``.
    """
    if not file_path:
        return PromptInfo(error="No file selected.")
    path = os.path.abspath(str(file_path))
    if not os.path.isfile(path):
        return PromptInfo(error="File does not exist.")

    fp = _file_fingerprint(path)
    cache_key = os.path.normcase(path)

    if use_cache:
        with _cache_lock:
            hit = _cache.get(cache_key)
            if hit is not None and hit[0] == fp:
                return hit[1]

    try:
        raw = extract_raw_metadata(path)
        info = parse_prompt_from_metadata(raw)
    except Exception as exc:
        _log.warning("[PromptExtract] Failed for %s: %s", path, exc)
        info = PromptInfo(error=f"Prompt extraction failed: {exc}")

    if use_cache:
        with _cache_lock:
            _cache[cache_key] = (fp, info)
            # Soft cap
            if len(_cache) > 256:
                try:
                    _cache.pop(next(iter(_cache)))
                except Exception:
                    pass

    return info


def clear_prompt_cache() -> None:
    with _cache_lock:
        _cache.clear()


def iter_ref_token_spans(text: str) -> list[tuple[int, int, str]]:
    """
    Return ``(start, end, canonical_token)`` character spans for reference tokens.

    ``canonical_token`` is lowercased with normalized spacing, e.g. ``picture 1``.
    """
    if not text:
        return []
    spans: list[tuple[int, int, str]] = []
    for m in _REF_TOKEN_RE.finditer(text):
        inner = re.sub(r"\s+", " ", m.group(1).strip().lower())
        # Skip huge accidental captures / non-ref markup
        if len(inner) > 48:
            continue
        spans.append((m.start(), m.end(), inner))
    return spans


def color_for_ref_token(canonical_token: str) -> str:
    """Stable color for a reference token key (same token → same color)."""
    if not canonical_token:
        return REF_COLOR_PALETTE[0]

    m = re.match(
        r"(picture|image|img|video|subject|environment|composition(?:_ref)?)\s*(\d+)?$",
        canonical_token,
    )
    if m:
        kind = m.group(1)
        num = int(m.group(2) or "1")
        kind_offset = {
            "picture": 0,
            "image": 0,
            "img": 0,
            "video": 5,
            "subject": 8,
            "environment": 11,
            "composition": 14,
            "composition_ref": 14,
        }.get(kind, 0)
        return REF_COLOR_PALETTE[(kind_offset + max(num, 1) - 1) % len(REF_COLOR_PALETTE)]

    h = 2166136261
    for ch in canonical_token:
        h ^= ord(ch)
        h = (h * 16777619) & 0xFFFFFFFF
    return REF_COLOR_PALETTE[h % len(REF_COLOR_PALETTE)]


def prompt_text_for_diff(info: PromptInfo) -> str:
    """Body text used for compare (positive + optional negative; no Model/Source header)."""
    if not info or not info.found:
        return ""
    parts: list[str] = []
    if info.positive:
        parts.append(info.positive.replace("\r\n", "\n").replace("\r", "\n").strip())
    if info.negative:
        parts.append("")
        parts.append("--- Negative ---")
        parts.append(info.negative.replace("\r\n", "\n").replace("\r", "\n").strip())
    return "\n".join(parts).strip()


_TOKEN_RE = re.compile(r"\s+|\w+|[^\w\s]")


def _tokenize_with_spans(text: str) -> list[tuple[int, int, str]]:
    """Return ``(start, end, token)`` covering the whole string."""
    return [(m.start(), m.end(), m.group(0)) for m in _TOKEN_RE.finditer(text or "")]


def _merge_spans(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    if not spans:
        return []
    spans = sorted(spans)
    out = [spans[0]]
    for start, end in spans[1:]:
        ps, pe = out[-1]
        if start <= pe:
            out[-1] = (ps, max(pe, end))
        else:
            out.append((start, end))
    return out


def inline_change_spans(old: str, new: str) -> tuple[list[tuple[int, int]], list[tuple[int, int]]]:
    """
    Word-level (with char refine) highlight ranges inside ``old`` / ``new``.

    Offsets are relative to the bare line content (no ``+/-`` prefix).
    """
    ta = _tokenize_with_spans(old)
    tb = _tokenize_with_spans(new)
    if not ta and not tb:
        return [], []

    sm = SequenceMatcher(
        None,
        [t[2] for t in ta],
        [t[2] for t in tb],
        autojunk=False,
    )
    spans_a: list[tuple[int, int]] = []
    spans_b: list[tuple[int, int]] = []

    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue

        # Single-token replace → refine to characters (e.g. motion3 vs motion)
        if tag == "replace" and (i2 - i1) == 1 and (j2 - j1) == 1:
            oa, ea, sa = ta[i1]
            ob, eb, sb = tb[j1]
            csm = SequenceMatcher(None, sa, sb, autojunk=False)
            for ctag, ci1, ci2, cj1, cj2 in csm.get_opcodes():
                if ctag in ("replace", "delete") and ci1 != ci2:
                    spans_a.append((oa + ci1, oa + ci2))
                if ctag in ("replace", "insert") and cj1 != cj2:
                    spans_b.append((ob + cj1, ob + cj2))
            continue

        if tag in ("replace", "delete"):
            for k in range(i1, i2):
                spans_a.append((ta[k][0], ta[k][1]))
        if tag in ("replace", "insert"):
            for k in range(j1, j2):
                spans_b.append((tb[k][0], tb[k][1]))

    return _merge_spans(spans_a), _merge_spans(spans_b)


def _pair_inline_spans(
    rows: list[tuple[str, str]],
) -> list[tuple[str, str, list[tuple[int, int]]]]:
    """
    Attach inline highlight spans to del/add lines by pairing consecutive blocks.

    Each result row is ``(kind, line, spans)`` where spans are offsets into ``line``
    (including the leading ``+``/``-`` when present).
    """
    out: list[tuple[str, str, list[tuple[int, int]]]] = []
    i = 0
    n = len(rows)
    while i < n:
        kind, line = rows[i]
        if kind != "del":
            out.append((kind, line, []))
            i += 1
            continue

        dels: list[tuple[str, str]] = []
        while i < n and rows[i][0] == "del":
            dels.append(rows[i])
            i += 1
        adds: list[tuple[str, str]] = []
        while i < n and rows[i][0] == "add":
            adds.append(rows[i])
            i += 1

        # Pair by index; leftover pure insert/delete get no inline spans
        paired = min(len(dels), len(adds))
        del_spans: list[list[tuple[int, int]]] = [[] for _ in dels]
        add_spans: list[list[tuple[int, int]]] = [[] for _ in adds]

        for k in range(paired):
            old_body = dels[k][1][1:] if dels[k][1].startswith("-") else dels[k][1]
            new_body = adds[k][1][1:] if adds[k][1].startswith("+") else adds[k][1]
            sa, sb = inline_change_spans(old_body, new_body)
            # Shift by 1 for the +/- prefix
            del_spans[k] = [(s + 1, e + 1) for s, e in sa]
            add_spans[k] = [(s + 1, e + 1) for s, e in sb]

        for k, (dk, dline) in enumerate(dels):
            out.append((dk, dline, del_spans[k]))
        for k, (ak, aline) in enumerate(adds):
            out.append((ak, aline, add_spans[k]))

    return out


def build_unified_prompt_diff(
    text_a: str,
    text_b: str,
    *,
    name_a: str = "a",
    name_b: str = "b",
    context: int = 3,
) -> list[tuple[str, str, list[tuple[int, int]]]]:
    """
    Build a unified diff as ``(kind, line, inline_spans)`` rows.

    ``kind`` is one of: ``header``, ``hunk``, ``equal``, ``del``, ``add``, ``meta``.
    ``inline_spans`` mark word/char changes within del/add lines (offsets into ``line``).
    """
    a_lines = (text_a or "").splitlines()
    b_lines = (text_b or "").splitlines()

    if a_lines == b_lines:
        return [
            ("meta", f"No differences — {name_a} and {name_b} match.", []),
        ]

    raw = unified_diff(
        a_lines,
        b_lines,
        fromfile=name_a,
        tofile=name_b,
        lineterm="",
        n=context,
    )

    rows: list[tuple[str, str]] = []
    for line in raw:
        if line.startswith("--- ") or line.startswith("+++ "):
            rows.append(("header", line))
        elif line.startswith("@@"):
            rows.append(("hunk", line))
        elif line.startswith("-"):
            rows.append(("del", line))
        elif line.startswith("+"):
            rows.append(("add", line))
        elif line.startswith(" "):
            rows.append(("equal", line))
        else:
            rows.append(("meta", line))

    if not rows:
        return [("meta", "No differences.", [])]
    return _pair_inline_spans(rows)
