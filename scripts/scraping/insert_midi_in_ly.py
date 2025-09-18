#!/usr/bin/env python3
"""Insert \midi blocks into every \score in a LilyPond file and set default MIDI instruments.

Usage: run this script with no arguments. It will modify the file at
../../data/realbook/openbook.ly relative to this script's directory.

Behavior:
- Creates a backup `${target}.bak` before modifying.
- Idempotent: will not duplicate existing \midi blocks.
- Ensures the following instrument defaults are present (uncommented):
    \set ChordNames.midiInstrument = #"acoustic grand"
    \set Staff.midiInstrument = #"acoustic grand"
    \set PianoStaff.instrumentName = #"acoustic grand"
- Inserts a minimal `\midi { }` block after an existing `\layout { ... }` block
  if present, otherwise just before the closing brace of each `\score { ... }`.
"""
from __future__ import annotations
import os
import re
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
TARGET = (SCRIPT_DIR / "../../data/realbook/openbook.ly").resolve()
BACKUP = TARGET.with_suffix(TARGET.suffix + ".bak")

MIDI_BLOCK = """\
\n\midi {
  % tempo is taken from \tempo markings inside each score
}\n"""

INSTR_REPLACEMENTS = [
    # (regex pattern, replacement line)
    (r"^\s*%?\\set\s+ChordNames\.midiInstrument\s*=.*$",
     "\\set ChordNames.midiInstrument = #\"acoustic grand\""),
    (r"^\s*%?\\set\s+Staff\.midiInstrument\s*=.*$",
     "\\set Staff.midiInstrument = #\"acoustic grand\""),
    (r"^\s*%?\\set\s+PianoStaff\.instrumentName\s*=.*$",
     "\\set PianoStaff.instrumentName = #\"acoustic grand\""),
]

SCORE_START_RE = re.compile(r"\\score\s*\{")
MIDI_PRESENT_RE = re.compile(r"\\midi\s*\{", re.IGNORECASE)
LAYOUT_START_RE = re.compile(r"\\layout\s*\{")


def read_text(path: Path) -> str:
    with path.open("r", encoding="utf-8") as f:
        return f.read()


def write_text(path: Path, text: str) -> None:
    with path.open("w", encoding="utf-8") as f:
        f.write(text)


def find_matching_brace(text: str, open_pos: int) -> int:
    """Given index of '{', return index of the matching '}' (inclusive).
    Raises ValueError if not balanced.
    """
    depth = 0
    for i in range(open_pos, len(text)):
        c = text[i]
        if c == '{':
            depth += 1
        elif c == '}':
            depth -= 1
            if depth == 0:
                return i
    raise ValueError("Unbalanced braces starting at position %d" % open_pos)


def find_block(text: str, start_pat: re.Pattern, start_idx: int = 0):
    """Find first occurrence of a command block like \layout { ... } starting at start_idx.
    Returns (cmd_start, block_open_brace, block_close_brace), or None if not found.
    """
    m = start_pat.search(text, pos=start_idx)
    if not m:
        return None
    # find the '{' after the command
    brace_idx = text.find('{', m.end()-1)
    if brace_idx == -1:
        return None
    close_idx = find_matching_brace(text, brace_idx)
    return (m.start(), brace_idx, close_idx)


def insert_midi_in_score_block(score_block: str) -> str:
    # If it already has a \midi block, return unchanged
    if MIDI_PRESENT_RE.search(score_block):
        return score_block

    # Try to locate \layout { ... } inside this score block
    # We search within the block text only
    layout = find_block(score_block, LAYOUT_START_RE, 0)
    if layout:
        # Insert MIDI right after the layout block
        _, _, layout_close = layout
        return score_block[:layout_close+1] + MIDI_BLOCK + score_block[layout_close+1:]

    # Otherwise, insert just before the final closing '}' of the score
    # Find the last non-space character index that should be '}'
    last_close = len(score_block) - 1
    # Ensure we are at a closing brace
    while last_close >= 0 and score_block[last_close].isspace():
        last_close -= 1
    if last_close < 0 or score_block[last_close] != '}':
        # Fallback: append at end
        return score_block + MIDI_BLOCK
    return score_block[:last_close] + MIDI_BLOCK + score_block[last_close:]


def process_scores(full_text: str) -> tuple[str, int]:
    """Insert MIDI blocks into each \score { ... } and return (new_text, count_inserted)."""
    out_parts = []
    idx = 0
    inserted = 0
    while True:
        m = SCORE_START_RE.search(full_text, pos=idx)
        if not m:
            out_parts.append(full_text[idx:])
            break
        # copy text before this score
        out_parts.append(full_text[idx:m.start()])
        # find block start '{' and its matching '}'
        brace_open = full_text.find('{', m.end()-1)
        if brace_open == -1:
            # malformed, copy rest and stop
            out_parts.append(full_text[m.start():])
            break
        brace_close = find_matching_brace(full_text, brace_open)
        score_block = full_text[m.start():brace_close+1]
        new_block = insert_midi_in_score_block(score_block)
        if new_block != score_block:
            inserted += 1
        out_parts.append(new_block)
        idx = brace_close + 1
    return ("".join(out_parts), inserted)


def normalize_instrument_lines(text: str) -> tuple[str, int]:
    """Ensure instrument lines exist in uncommented form; add if missing."""
    replacements = 0
    lines = text.splitlines()
    present = {key: False for _, key in zip(INSTR_REPLACEMENTS, [
        "ChordNames", "Staff", "PianoStaff"
    ])}

    # First pass: regex-replace existing (commented or not)
    for i, line in enumerate(lines):
        for pat, repl in INSTR_REPLACEMENTS:
            if re.match(pat, line):
                if line.strip() != repl:
                    lines[i] = repl
                    replacements += 1
                present_key = ("ChordNames" if "ChordNames" in repl else
                               "Staff" if "Staff.midiInstrument" in repl else
                               "PianoStaff")
                present[present_key] = True
                break

    # Second pass: if any are missing entirely, insert them after the
    # comment header or version line near the top (after first non-empty line)
    header_insert_idx = 0
    for j, ln in enumerate(lines[:80]):  # only scan early region
        if ln.strip().startswith("\\version"):
            header_insert_idx = j + 1
            break

    to_add = []
    if not present["ChordNames"]:
        to_add.append("\\set ChordNames.midiInstrument = #\"acoustic grand\"")
    if not present["Staff"]:
        to_add.append("\\set Staff.midiInstrument = #\"acoustic grand\"")
    if not present["PianoStaff"]:
        to_add.append("\\set PianoStaff.instrumentName = #\"acoustic grand\"")

    if to_add:
        lines[header_insert_idx:header_insert_idx] = to_add
        replacements += len(to_add)

    return ("\n".join(lines) + ("\n" if not lines or not lines[-1].endswith("\n") else ""), replacements)


def main():
    if not TARGET.exists():
        raise SystemExit(f"Target file not found: {TARGET}")

    original = read_text(TARGET)

    # Normalize instrument lines
    with_instr, instr_changes = normalize_instrument_lines(original)

    # Insert MIDI blocks into scores
    updated, midi_inserted = process_scores(with_instr)

    if updated == original:
        print("No changes needed. File already contains instruments and MIDI blocks.")
        return

    # Write backup then updated file
    write_text(BACKUP, original)
    write_text(TARGET, updated)

    print(f"Updated: {TARGET}")
    print(f"Backup:  {BACKUP}")
    print(f"Instrument lines changed/added: {instr_changes}")
    print(f"MIDI blocks inserted: {midi_inserted}")


if __name__ == "__main__":
    main()
