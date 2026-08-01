# CLAUDE.md

Guidance for Claude Code when working in this repository.

## What this is

Read `docs/01-project-goals.md` first — it's the reading-order index for the entire `docs/`
set (goals, source/target signals, mapping, battery-management/safety layer, real-time engine,
startup/shutdown, GUI design, STM32 export format, open questions, verification checklist).

In short: a Python GUI app that bridges a Lexus RZ450e HV battery to a Nissan Leaf's CAN bus,
acting as both a live translator and a configurable battery-management layer — not a passive
protocol relay.

## `Refrance/` is read-only reference material

`Refrance/Leaf_BMS_Emulator/` and `Refrance/RZ450e_battery_can_decode_Project/` are two other,
independent Claude Code projects (each with their own git repo), kept here purely for reference.
**Never edit, move, or commit changes inside `Refrance/`.** If something in this project's `docs/`
turns out to be wrong and the live reference project has since corrected it, update this project's
docs to match — never the other way around.

## Revision numbering (once app code exists)

Follow the same convention both reference projects use: a single `REVISION`/`APP_REVISION`
constant plus a docstring changelog at the top of the main app file, bumped on every change, with
enough detail to explain *what was measured/verified/changed and why* — not just "fixed bug." This
changelog becomes the project's primary history, more detailed than git log. (This is also
required by the user's global Claude Code instructions for any project with a versioned main
file.)

## Conventions to carry forward from both reference projects

- **Confirmed vs. unverified discipline**: never silently upgrade an "unverified"/"documented"
  item to "confirmed" — that's a deliberate human sign-off step (see
  `docs/11-manual-verification-checklist.md`), not something to infer from code behavior alone.
- **Curated, named features over generic scripting**: the mapping engine and battery-management
  layer are both explicitly designed as fixed sets of named, config-driven features — not
  general-purpose rule/expression engines — so they stay portable to the future STM32 firmware
  port. Don't introduce a generic scripting/expression escape hatch without discussing it first.
- **Safety-relevant numbers need a real source**: when adding or changing a threshold in
  `docs/05-battery-management-safety.md`, cite where the number came from (real-hardware test,
  researched industry reference, or explicit user instruction) — don't invent a "reasonable-
  looking" safety number.
- **Write analysis/verification scripts into the repo** (a `tests/` or similar folder once one
  exists), not into a scratchpad — this project follows both reference projects' pattern of citing
  test scripts from reports/docs and re-running them in later sessions.

## Before committing or pushing

Never commit or push without the user explicitly asking, even after a large multi-file change —
same rule both reference projects use.

## Memory & continuity across sessions

This project's own auto-memory (`~/.claude/projects/.../memory/MEMORY.md` and linked files) tracks
scope decisions and context that aren't derivable from the docs alone (e.g. *why* a design choice
was made, what the user has and hasn't confirmed on real hardware yet). Check it before assuming
you need to re-derive something already decided in a prior session — but verify specific claims
about code/files against the current repo state, since memory is frozen at write time.
