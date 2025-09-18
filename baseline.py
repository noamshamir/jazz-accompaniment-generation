#!/usr/bin/env python3
"""
Add a super-simple quarter-note bassline to Real Book MIDIs by arpeggiating the
chord track ("Piano, Chords:") up and down until the chord changes.

Input  (relative to this script):  ../../data/realbook/midis/
Files: openbook-1.midi ... openbook-155.midi
Output: ../../data/realbook/midis_with_bass/openbook-<n>_with_bass.mid

Rules
-----
- Identify the instrument named exactly starting with: "Piano, Chords:"
  (melody track "Piano, Melody:Voice" is ignored).
- Every beat (quarter note) place ONE bass note.
- While the chord (set of pitch classes sounding in the chords track) remains the
  same, walk *up* its chord tones one by one each beat, then *down* when you hit
  the top (ping-pong). When the chord changes, reset to the bottom again.
- Keep bass in C1–C3 by choosing the closest octave to the prior note.
- Use Acoustic Bass (GM program 33), velocity ~85.

Requires: pip install pretty_midi
"""
from __future__ import annotations
import os
from pathlib import Path
from typing import List, Optional, Tuple

import pretty_midi
import re

VERBOSE = True

# --- Paths ---
SCRIPT_DIR = Path(__file__).resolve().parent
IN_DIR = (SCRIPT_DIR / "../../data/realbook/midis").resolve()
OUT_DIR = (SCRIPT_DIR / "../../data/realbook/midis_with_bass").resolve()
OUT_DIR.mkdir(parents=True, exist_ok=True)

LY_PATH = (SCRIPT_DIR / "../../data/realbook/openbook.ly").resolve()

# --- Constants ---
BASS_PROGRAM = 33  # Acoustic Bass
VEL = 85
C1, C3 = 24, 48
NOTE_DENSITY = 1 # 1 = quarter notes, 2 = eighth notes
CHORD_VEL_SCALE = 0.7  # make chord line softer (0..1)
MELODY_SAX_PROGRAM = 65  # GM Alto Sax (0-based: 64 = program 65 one-based)

# If the piece is in cut time (2/2), we double density so it feels like 4/4
# relative to quarter-note subdivisions.
def effective_note_density(pm: pretty_midi.PrettyMIDI) -> int:
    dens = NOTE_DENSITY
    ts_changes = pm.time_signature_changes
    denom = None
    if ts_changes:
        # Use the first time signature encountered (typical for these files)
        denom = ts_changes[0].denominator
    if denom == 2:
        dens *= 2
    return dens

TOC_RE = re.compile(r"\\tocItem\s+\\markup\s+\"([^\"]+)\"")

def parse_toc_titles(ly_path: Path) -> list[str]:
    """Parse \tocItem titles from the LilyPond file, in order."""
    try:
        txt = ly_path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return []
    return TOC_RE.findall(txt)

def slugify(title: str, max_len: int = 80) -> str:
    """Make a filesystem-friendly filename slug from a title."""
    import re as _re
    # Replace slashes with dash, remove illegal characters, collapse spaces/underscores
    t = title.replace('/', '-').replace(':', '-')
    t = _re.sub(r"[^A-Za-z0-9\-\s_]+", "", t)
    t = _re.sub(r"\s+", "_", t).strip("_")
    if len(t) > max_len:
        t = t[:max_len].rstrip("_-")
    return t or "untitled"


# --- Helpers ---

def pc_to_midi_near(pc: int, ref_midi: int) -> int:
    """Place pitch class near ref MIDI, clamped to C1..C3 by octave shifts."""
    if ref_midi is None:
        # Default around C2
        base = 36  # C2
    else:
        base = ref_midi
    # choose octave that minimizes distance
    candidates = []
    for octv in range(0, 10):
        m = pc + 12 * octv
        candidates.append(m)
    # choose nearest to base then clamp into range by 12s
    chosen = min(candidates, key=lambda m: abs(m - base))
    while chosen < C1:
        chosen += 12
    while chosen > C3:
        chosen -= 12
    return chosen


def notes_overlapping(instrument: pretty_midi.Instrument, start: float, end: float) -> List[pretty_midi.Note]:
    return [n for n in instrument.notes if n.start < end and n.end > start]


def chord_pitch_classes(notes: List[pretty_midi.Note]) -> List[int]:
    pcs = sorted({n.pitch % 12 for n in notes})
    return pcs


def avg_simultaneity(inst: pretty_midi.Instrument, slices: int = 512) -> float:
    """Rough polyphony estimate: average number of overlapping notes across timeline."""
    if not inst.notes:
        return 0.0
    t0 = min(n.start for n in inst.notes)
    t1 = max(n.end for n in inst.notes)
    if t1 <= t0:
        return 0.0
    import numpy as np
    times = np.linspace(t0, t1, slices)
    tot = 0.0
    for i in range(len(times) - 1):
        a, b = float(times[i]), float(times[i+1])
        tot += sum(1 for n in inst.notes if n.start < b and n.end > a)
    return tot / (len(times) - 1)


def scale_instrument_velocity(inst: pretty_midi.Instrument, scale: float) -> None:
    for n in inst.notes:
        n.velocity = max(1, min(127, int(round(n.velocity * scale))))


def retarget_melody_to_sax(pm: pretty_midi.PrettyMIDI, melody_name_hint: str = "Melody") -> None:
    # Find the melody instrument by name hint; fall back to the least polyphonic non-drum track
    target = None
    for inst in pm.instruments:
        if inst.name and melody_name_hint.lower() in inst.name.lower():
            target = inst
            break
    if target is None:
        # fallback: pick instrument with fewest simultaneous overlaps but with many notes (likely monophonic melody)
        best, best_score = None, float('inf')
        for inst in pm.instruments:
            if inst.is_drum or not inst.notes:
                continue
            score = avg_simultaneity(inst)
            if score < best_score:
                best, best_score = inst, score
        target = best
    if target is not None:
        target.program = MELODY_SAX_PROGRAM
        if VERBOSE:
            print(f"[melody] Retargeted '{target.name or f'prog:{target.program}'}' to Alto Sax (program {MELODY_SAX_PROGRAM})")


def ping_pong_indices(n: int) -> List[int]:
    """Return index order 0,1,...,n-1,n-2,...,1 repeating (length unbounded).
    We'll advance a pointer through this pattern; implementation handled via direction flag.
    """
    return list(range(n))  # we’ll handle direction separately


def add_bassline(pm: pretty_midi.PrettyMIDI,
                 chord_instr_name_prefix: str = "Piano, Chords:") -> pretty_midi.PrettyMIDI:
    # Find chords instrument
    chords_inst = None
    # 1) Exact prefix match
    for inst in pm.instruments:
        if inst.name and inst.name.startswith(chord_instr_name_prefix):
            chords_inst = inst
            break
    # 2) Fallback: substring "Chords" (case-insensitive)
    if chords_inst is None:
        for inst in pm.instruments:
            if inst.name and ("chords" in inst.name.lower()):
                chords_inst = inst
                break
    # 3) Fallback: pick the most polyphonic instrument (likely chord track)
    if chords_inst is None:
        if VERBOSE:
            print("[info] Falling back to most-polyphonic instrument…")
        best, best_score = None, -1.0
        for inst in pm.instruments:
            score = avg_simultaneity(inst)
            if score > best_score and inst.notes:
                best, best_score = inst, score
        chords_inst = best
    if chords_inst is None or not chords_inst.notes:
        if VERBOSE:
            names = [getattr(i, 'name', '') or f'prog:{i.program}' for i in pm.instruments]
            print(f"[warn] No chords instrument found; instruments: {names}")
        return pm
    if VERBOSE:
        print(f"[ok] Using chords instrument: '{chords_inst.name or f'prog:{chords_inst.program}'}'")
    # Make the chord line a bit softer
    scale_instrument_velocity(chords_inst, CHORD_VEL_SCALE)

    # Beat grid (quarter notes). pretty_midi.get_beats returns beat times respecting tempo map
    beats = pm.get_beats()
    if len(beats) < 2:
        # fallback: try downbeats or infer from the notes
        if pm.get_downbeats().size >= 2:
            beats = pm.get_downbeats()
        else:
            # crude fallback from first to last note times
            t0 = min(n.start for n in chords_inst.notes)
            t1 = max(n.end for n in chords_inst.notes)
            # assume 120 bpm quarters
            import numpy as np
            beats = np.arange(t0, t1, 0.5)

    dens = effective_note_density(pm)
    if dens > 1:
        import numpy as np
        new_beats = []
        for i in range(len(beats) - 1):
            seg = np.linspace(beats[i], beats[i+1], dens + 1)
            new_beats.extend(seg[:-1])
        new_beats.append(beats[-1])
        beats = new_beats

    # Prepare bass instrument
    bass = pretty_midi.Instrument(program=BASS_PROGRAM, name="Walking Bass (auto)")

    # Walk state
    prev_pcset: Optional[Tuple[int, ...]] = None
    order: List[int] = []  # ordered pitch classes for current chord
    idx = 0
    dir_up = True
    prev_midi: Optional[int] = None

    # Iterate beat-to-beat
    for bi in range(len(beats) - 1):
        t0, t1 = float(beats[bi]), float(beats[bi + 1])
        win_notes = notes_overlapping(chords_inst, t0, t1)
        pcs = chord_pitch_classes(win_notes)
        if not pcs:
            # sustain previous choice if silence
            pcs_tuple = prev_pcset
            if pcs_tuple is None:
                continue
        else:
            pcs_tuple = tuple(pcs)

        # If chord changed, reset to bottom of new chord ascending
        if pcs_tuple != prev_pcset:
            order = list(pcs_tuple)
            idx = 0
            dir_up = True
            prev_pcset = pcs_tuple

        # Choose current pc and map near previous MIDI into bass range
        pc = order[idx]
        midi_pitch = pc_to_midi_near(pc, prev_midi if prev_midi is not None else 36)

        # Add bass note
        # Shorten slightly to avoid overlaps
        dur = (t1 - t0) * 0.98
        bass.notes.append(pretty_midi.Note(velocity=VEL, pitch=midi_pitch, start=t0, end=t0 + dur))

        prev_midi = midi_pitch

        # Advance ping-pong index for next beat
        if dir_up:
            if idx + 1 < len(order):
                idx += 1
            else:
                dir_up = False
                if len(order) > 1:
                    idx -= 1
        else:
            if idx - 1 >= 0:
                idx -= 1
            else:
                dir_up = True
                if len(order) > 1:
                    idx += 1

    pm.instruments.append(bass)
    return pm


def process_all():
    import re

    # Files named openbook-<n>.midi, n in 1..155
    titles = parse_toc_titles(LY_PATH)
    if VERBOSE:
        print(f"[titles] parsed {len(titles)} titles from {LY_PATH.name}")

    for n in range(1, 156):
        src = IN_DIR / f"openbook-{n}.midi"
        if not src.exists():
            # Also support .mid just in case
            alt = IN_DIR / f"openbook-{n}.mid"
            if alt.exists():
                src = alt
            else:
                print(f"[skip] missing {src}")
                continue
        try:
            pm = pretty_midi.PrettyMIDI(str(src))
            if VERBOSE:
                print(f"[file] {src.name} instruments:", [getattr(i, 'name', '') or f'prog:{i.program}' for i in pm.instruments])
                if VERBOSE:
                    sigs = [(f"{ts.numerator}/{ts.denominator}", round(ts.time, 3)) for ts in pm.time_signature_changes]
                    print("[ts]", sigs if sigs else "none", "| effective density:", effective_note_density(pm))
            # Change head/melody to saxophone
            retarget_melody_to_sax(pm, melody_name_hint="Melody")
        except Exception as e:
            print(f"[error] loading {src}: {e}")
            continue

        pm = add_bassline(pm, chord_instr_name_prefix="Piano, Chords:")

        added = 0
        if pm.instruments:
            # our bass is appended as last instrument if added
            last = pm.instruments[-1]
            if last.name and last.name.startswith("Walking Bass"):
                added = len(last.notes)
        if VERBOSE:
            print(f"[bass] notes added: {added}")

        if 1 <= n <= len(titles):
            # Keep only the song title before the '/' (ignore artist list)
            title_only = titles[n-1].split('/')[0].strip()
            safe = slugify(title_only)
            dst_name = f"{safe}.mid"
        else:
            dst_name = f"openbook-{n}.mid"
        dst = OUT_DIR / dst_name
        try:
            pm.write(str(dst))
            print(f"[ok] {dst}")
        except Exception as e:
            print(f"[error] writing {dst}: {e}")


if __name__ == "__main__":
    process_all()
