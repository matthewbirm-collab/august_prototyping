# propline-fantasy

Market-implied NFL fantasy projections built from sportsbook player-prop lines, with line movement as an early signal.

## Using this kit with Claude Code

1. Copy this folder into a new repo (or unzip it there) and `git init`.
2. Open the folder in Claude Code. `CLAUDE.md` loads automatically.
3. Resolve the open decisions at the bottom of `docs/ROADMAP.md` (or let Claude ask you as it goes).
4. Run `/build-next`. Claude picks the first unchecked step, proposes a plan, waits for your approval, builds it with tests, and checks it off.
5. Repeat. Use `/status` any time for progress, gate checks and rule drift.

You can also run any step directly, e.g. `/p3-distributions`.

## Files

| Path | Purpose |
| --- | --- |
| `CLAUDE.md` | Project brief, rules, defaults and working agreement loaded into every session |
| `docs/PLAN.md` | Concept, data sources, architecture, projection math, risks |
| `docs/ROADMAP.md` | Ordered build steps with phase gates and open decisions |
| `.claude/commands/` | One slash command per build step, plus `/build-next` and `/status` |

## Data attribution

NFL data from [nflverse](https://github.com/nflverse) (CC-BY 4.0; FTN charting data CC-BY-SA 4.0). Odds data from The Odds API, subject to its terms.

Projections only — not betting advice.
