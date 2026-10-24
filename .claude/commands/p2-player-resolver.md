---
description: Phase 2 — map sportsbook player names to canonical player IDs
---
Follow the working agreement in `CLAUDE.md`. Roadmap step: `/p2-player-resolver`.

Sportsbooks send player names as free text. Build `backend/ingest/resolver.py` that maps (raw_name, team, position hint, game) to a canonical `player_id`.

Matching order:
1. Exact hit in `player_aliases`.
2. Normalized name (strip Jr./Sr./II/III, punctuation, accents, case) among rostered players on the two teams in that game.
3. Fuzzy match (rapidfuzz, threshold `FUZZY_MATCH_THRESHOLD`) restricted to those rosters.
4. Otherwise write to `unmatched_names`.

Persist every successful match as a `player_alias`. Add an admin CLI to review and confirm unmatched names. Report the weekly unmatched rate.

Tests for tricky cases: shared last names on one team, name changes, suffixes, initials ("D.J."), and a player traded mid-season.

If `p2-nflverse` is not done yet, stub the roster lookup behind an interface and note it.
