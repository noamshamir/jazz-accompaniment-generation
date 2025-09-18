import os, json, csv, shutil, subprocess, uuid, re
from pathlib import Path
from urllib.parse import urlparse
import requests
import pandas as pd
from tqdm import tqdm
from music21 import converter, midi
from concurrent.futures import ProcessPoolExecutor, as_completed

# --- Chord injection helpers ---
CHORD_RE = re.compile(
    r"^[A-G](?:#|b)?"
    r"(?:maj7|maj9|maj|min7|min9|min|m7|m9|m|dim7|dim|aug|sus2|sus4|add[24679]|7|9|11|13|6|°|ø|Δ)?"
    r"(?:\([^)]+\))?"
    r"(?:/[A-G](?:#|b)?)?$"
)

def _is_chord_like(text: str) -> bool:
    if not text:
        return False
    t = text.strip()
    # Common cleanups
    t = t.replace("Maj", "maj").replace("Min", "min")
    return bool(CHORD_RE.match(t))

def inject_chord_symbols_from_text(xml_path: Path) -> int:
    """
    Parse MusicXML and convert text expressions that look like chord symbols
    into proper &lt;harmony&gt; entries at their offsets. Overwrites xml_path in place.
    Returns the count of injected chord symbols.
    """
    from music21 import converter, expressions, harmony

    try:
        s = converter.parse(str(xml_path))
    except Exception:
        return 0

    # Count existing harmony first
    existing = sum(1 for _ in s.recurse().getElementsByClass('Harmony'))
    injected = 0

    # Look for text expressions (often what OCR exports for chords)
    for te in s.recurse().getElementsByClass(expressions.TextExpression):
        txt = (getattr(te, "content", None) or "").strip()
        if not _is_chord_like(txt):
            continue
        try:
            cs = harmony.ChordSymbol(txt)
            # Place a Harmony at the same absolute offset
            s.insert(te.getOffsetInHierarchy(s), cs)
            injected += 1
        except Exception:
            continue

    if injected:
        # Write back to the same file path, upgrading text → harmony
        s.write("musicxml", fp=str(xml_path))

    return injected

def ensure_dir(p: Path):
    p.mkdir(parents=True, exist_ok=True)

def download_url_or_copy(url_or_path: str, out_dir: Path) -> Path:
    s = str(url_or_path)
    ensure_dir(out_dir)
    if s.startswith(("http://", "https://")):
        # keep original extension when possible; default .png
        ext = os.path.splitext(urlparse(s).path)[1] or ".png"
        name = os.path.basename(urlparse(s).path) or f"img_{uuid.uuid4().hex}{ext}"
        out = out_dir / name
        r = requests.get(s, timeout=30)
        r.raise_for_status()
        with open(out, "wb") as f:
            f.write(r.content)
        return out
    src = Path(s)
    if not src.exists():
        raise FileNotFoundError(f"Not found: {src}")
    out = out_dir / src.name
    if src.resolve() != out.resolve():
        shutil.copy2(src, out)
    return out

def run_audiveris(audiveris_bin: str, image_path: Path, out_xml_root: Path, log_dir: Path) -> Path | None:
    # NOTE: We intentionally do NOT create or write logs; we only want XML/MusicXML output.
    ensure_dir(out_xml_root)
    # Output will be in <out_xml_root>/<stem>/*.xml
    cmd = [
        audiveris_bin,
        "-batch",
        "-export",
        "-output", str(out_xml_root),
        str(image_path)
    ]
    # Discard stdout/stderr rather than writing .log files
    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, text=True)
    candidates_dir = out_xml_root / image_path.stem
    # prefer a file named exactly <stem>.xml, else first .xml
    exact = candidates_dir / f"{image_path.stem}.xml"
    if exact.exists():
        return exact
    candidates = list(candidates_dir.glob("*.xml"))
    return candidates[0] if candidates else None

def musicxml_to_midi(xml_path: Path, midi_path: Path):
    s = converter.parse(str(xml_path))
    mf = midi.translate.music21ObjectToMidiFile(s)
    midi_path.parent.mkdir(parents=True, exist_ok=True)
    mf.open(str(midi_path), "wb")
    mf.write()
    mf.close()

def synthesize_chord_track(xml_path: Path, midi_path_with_chords: Path, velocity: int = 64):
    # Optional: create a simple comping track from MusicXML harmony symbols
    from music21 import stream, chord
    s = converter.parse(str(xml_path))
    has_harmony = list(s.recurse().getElementsByClass('Harmony'))
    if not has_harmony:
        musicxml_to_midi(xml_path, midi_path_with_chords)
        return
    out = stream.Stream()
    out.insert(0, s)  # original content

    comp = stream.Part()
    comp.id = "ChordComp"
    qn = 1.0  # quarter-note duration
    for h in s.recurse().getElementsByClass('Harmony'):
        sym = h.figure  # ex: "C#7", "Amaj7"
        try:
            cs = chord.ChordSymbol(sym)
            if cs.pitchClasses:
                ch = chord.Chord(cs.pitches)
                ch.quarterLength = qn
                # place at harmony's absolute offset
                comp.insert(h.getOffsetInHierarchy(s), ch)
        except Exception:
            continue
    out.insert(0, comp)
    mf = midi.translate.music21ObjectToMidiFile(out)
    midi_path_with_chords.parent.mkdir(parents=True, exist_ok=True)
    mf.open(str(midi_path_with_chords), "wb")
    mf.write()
    mf.close()

def cleanup_aux_files(root: Path):
    """
    Remove any Audiveris project artifacts (.omr) and .log files under the given root.
    We keep only XML/MusicXML and MIDI outputs.
    """
    # Remove .omr directories or files
    for p in root.rglob("*.omr"):
        try:
            if p.is_dir():
                shutil.rmtree(p)
            else:
                p.unlink(missing_ok=True)
        except Exception:
            pass
    # Remove any .log files
    for p in root.rglob("*.log"):
        try:
            p.unlink(missing_ok=True)
        except Exception:
            pass

def process_one(image_spec: str, audiveris_bin: str, work: Path, force: bool, make_comp: bool, img_dir: Path, xml_dir: Path, midi_dir: Path) -> dict:
    ensure_dir(img_dir); ensure_dir(xml_dir); ensure_dir(midi_dir)

    try:
        img_path = download_url_or_copy(image_spec, img_dir)
    except Exception as e:
        return {"source": image_spec, "image": "", "xml": "", "midi": "", "status": f"download_error: {e}"}

    midi_out = midi_dir / f"{img_path.stem}.mid"
    xml_expected = xml_dir / img_path.stem / f"{img_path.stem}.xml"

    if midi_out.exists() and not force:
        return {"source": image_spec, "image": str(img_path), "xml": str(xml_expected if xml_expected.exists() else ""), "midi": str(midi_out), "status": "skipped_exists"}

    xml_path = xml_expected if xml_expected.exists() else run_audiveris(audiveris_bin, img_path, xml_dir, Path())
    # Clean any .omr or .log artifacts that Audiveris might have created
    cleanup_aux_files(xml_dir)
    if not xml_path or not xml_path.exists():
        return {"source": image_spec, "image": str(img_path), "xml": "", "midi": "", "status": "omr_failed"}

    # Attempt to convert chord-like text into proper &lt;harmony&gt; tags so chords "show"
    try:
        injected_count = inject_chord_symbols_from_text(xml_path)
    except Exception:
        injected_count = 0

    try:
        if make_comp:
            synthesize_chord_track(xml_path, midi_out)
        else:
            musicxml_to_midi(xml_path, midi_out)
        cleanup_aux_files(midi_dir.parent)  # clean under out_omr/*
        return {
            "source": image_spec,
            "image": str(img_path),
            "xml": str(xml_path),
            "midi": str(midi_out),
            "status": "ok" + (f"_chords_injected:{injected_count}" if injected_count else "")
        }
    except Exception as e:
        cleanup_aux_files(midi_dir.parent)  # clean under out_omr/*
        return {"source": image_spec, "image": str(img_path), "xml": str(xml_path), "midi": "", "status": f"midi_error: {e}"}

def _parallel_map(paths, worker, max_workers: int):
    results = []
    with ProcessPoolExecutor(max_workers=max_workers) as ex:
        futs = {ex.submit(worker, p): p for p in paths}
        for fut in tqdm(as_completed(futs), total=len(futs), desc="OMR parallel"):
            try:
                results.append(fut.result())
            except Exception as e:
                p = futs[fut]
                results.append({"source": str(p), "image": "", "xml": "", "midi": "", "status": f"worker_error: {e}"})
    return results

def process_dir(image_dir: str, max_workers: int, **kwargs) -> pd.DataFrame:
    exts = kwargs.pop("exts", (".png",".jpg",".jpeg",".pdf",".tif",".tiff"))
    paths = []
    for ext in exts:
        paths += [str(p) for p in Path(image_dir).glob(f"*{ext}")]
    if max_workers and max_workers > 1:
        results = _parallel_map(paths, lambda p: process_one(p, **kwargs), max_workers)
    else:
        results = [process_one(p, **kwargs) for p in tqdm(paths, desc="OMR dir")]
    return pd.DataFrame(results)

def process_csv(csv_path: str, image_col: str, max_workers: int, **kwargs) -> pd.DataFrame:
    df = pd.read_csv(csv_path)
    if image_col not in df.columns:
        raise ValueError(f"CSV must contain column '{image_col}'")
    items = df[image_col].astype(str).tolist()
    if max_workers and max_workers > 1:
        results = _parallel_map(items, lambda p: process_one(p, **kwargs), max_workers)
    else:
        results = [process_one(str(x), **kwargs) for x in tqdm(items, desc="OMR csv")]
    return pd.DataFrame(results)

def process_json(json_path: str, key: str, max_workers: int, **kwargs) -> pd.DataFrame:
    items = json.loads(Path(json_path).read_text(encoding="utf-8"))
    if isinstance(items, dict):
        items = items.get("items", [])
    if not isinstance(items, list):
        raise ValueError("JSON must be a list or have an 'items' list")
    sources = [str(it[key]) for it in items if key in it]
    if max_workers and max_workers > 1:
        results = _parallel_map(sources, lambda p: process_one(p, **kwargs), max_workers)
    else:
        results = [process_one(x, **kwargs) for x in tqdm(sources, desc="OMR json")]
    return pd.DataFrame(results)

def main():
    # Hard-coded configuration per user request (no CLI args)
    audiveris = "/Applications/Audiveris.app/Contents/MacOS/Audiveris"
    work = Path("out_omr")
    ensure_dir(work)

    img_dir = work / "images"
    xml_dir = work / "musicxml"
    midi_dir = work / "midi"

    # Common options: keep defaults (no force reprocess, no chord comp)
    common = dict(
        audiveris_bin=audiveris,
        work=work,
        force=False,
        make_comp=False,
        img_dir=img_dir,
        xml_dir=xml_dir,
        midi_dir=midi_dir,
    )

    # Always use CSV mode with given input and image column 'src'
    csv_path = "../../data/bopland/lick_imgs.csv"
    image_col = "src"

    # No parallelism change (max_workers=0 = sequential, minimal change from prior behavior)
    df = process_csv(csv_path, image_col=image_col, max_workers=0, **common)
    out = work / "index_from_csv.csv"

    df.to_csv(out, index=False)
    print(f"Wrote index: {out}")
    try:
        print(df.head(10).to_string(index=False))
    except Exception:
        print(df.head(10))

if __name__ == "__main__":
    main()