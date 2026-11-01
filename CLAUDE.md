# stillused

Claude Code skill and CLI that finds columns and tables migrations drop or rename while code still uses them.

## Architecture
`plugin/scripts/stillused.py` holds all the logic: `parse_*` read migrations, `search` scores each use in code, and `scan` turns them into findings. `plugin/skills/checking-schema-drops/SKILL.md` tells Claude how to use it. See `docs/architecture.md`.

## Commands
- `make test`: unit tests and project checks
- `make lint`: manifest validation
- `make demo`: scan `examples/shop`

## Conventions
- Python 3.10+, standard library only. Never connect to a database.
- High severity needs direct evidence: SQL naming the table, or `table.column` access. Everything else is medium.
- Each migration format has tests, including its down direction.

## Do and don't
- Do try rule changes on real repos and read what they flag.
- Don't say a drop is safe.

## Definition of done
`make test` and `make lint` pass, and `ROADMAP.md` and docs are updated when relevant.
