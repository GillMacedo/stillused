# stillused: find dropped columns your code still uses

[![CI](https://github.com/GillMacedo/stillused/actions/workflows/ci.yml/badge.svg)](https://github.com/GillMacedo/stillused/actions/workflows/ci.yml) [![MIT license](https://img.shields.io/badge/license-MIT-blue)](LICENSE)

**A Claude Code skill and CLI that finds columns and tables your database migrations drop or rename while code still uses the old name. It also flags releases that remove the code and the column together, which breaks pods still running the old code during a rollout.**

Migration linters like [squawk](https://github.com/sbdchd/squawk) and [strong_migrations](https://github.com/ankane/strong_migrations) read the migration, so they can tell you an operation is risky but not whether your code still needs the column. `stillused` compares the two. It reads files only and never connects to a database.

## What you can ask Claude

- "Is it safe to merge this migration that drops a column?"
- "Does anything still use `orders.discount_code`?"
- "I removed the code and the column in one PR. Is that OK?"
- "Help me rename this column without downtime."

## Install

As a Claude Code plugin:

    claude plugin marketplace add GillMacedo/stillused
    claude plugin install stillused@stillused-marketplace

Or run the script directly. It needs Python 3.10 or newer, with nothing from pip:

    git clone https://github.com/GillMacedo/stillused
    python3 stillused/plugin/scripts/stillused.py ./my-service --since origin/main

## Example

On the made-up service in `examples/shop`:

    $ make demo
    4 drops and renames in 1 migrations, compared with 1 code files

    HIGH   still-used: coupons
      migrations/0007_cleanup.sql
      This table is dropped, and code still uses the old name.
        app/orders.py:7

    HIGH   still-used: orders.discount_code
      migrations/0007_cleanup.sql
      This column is dropped, and code still uses the old name.
        app/orders.py:3

    HIGH   still-used: orders.total
      migrations/0007_cleanup.sql
      This column is renamed to total_cents, and code still uses the old name.
        app/orders.py:3

    No references found: customers.legacy_ref (dropped)

    This compares names in text, without a database. Code built at runtime and other services that share the database aren't visible.

## Choosing migrations

| Option | Checks |
|---|---|
| `--since REF` | Migrations changed since a git ref, and deploy order risks. Best for a pull request or CI |
| `--last N` | The newest N migrations. The default is 5 |
| `--file PATH` | One migration, repeatable |
| `--all` | Every migration, which is noisy because old names are often reused |

Add `--format markdown` or `--format json`. `--fail-on` sets the severity that exits 1: `high` by default, or `medium` or `none`. With no migrations found, it also exits 1, unless `--since` found no changes.

## Findings

| Finding | Severity | Meaning |
|---|---|---|
| `still-used` | High | SQL names the table and the old name, or the code reads it directly, like `order.gift_note` |
| `maybe-used` | Medium | The old name appears in code, but it could be something else |
| `schema-out-of-date` | Medium | Only an ORM schema file still has the old name |
| `deploy-order` | Medium | With `--since`: this change removed the code and the column together |

It reads SQL (including Prisma, goose and dbmate), Rails, Django, Alembic, Knex and TypeORM raw SQL migrations, using only the up direction. It also matches `snake_case` and `camelCase` spellings.

## Limits

- **No references doesn't mean safe.** It can't see other services, reports, dynamic queries or anything built at runtime.
- **It compares names in text.** A field with the same name on another model can show as `maybe-used`, and multi-line SQL often lands there too rather than as `still-used`.
- **Very common names,** like `status`, are ignored unless the table's name is on the same line.
- **Rails' `ignored_columns`** doesn't count as a use, because it's how a column is safely removed.
- Tests, `vendor`, `node_modules` and other migrations aren't searched.

## FAQ

**How do I drop a column without downtime?** Stop using it and ship that release, then drop it in a later one. `deploy-order` catches doing both at once.

**How do I rename a column safely?** Add the new one, write to both, backfill, switch reads, then drop the old one. `stillused` shows what still reads the old name.

**Does it replace squawk or strong_migrations?** No. They check whether the operation itself is safe, such as locks. Use them together.

**Is it an MCP server?** No. It's a skill with a bundled script, so it needs no server or package install.

## Tested on

Unit tests cover each migration format, and what each finding must and must not match. It also ran on recent migrations from `mastodon`, `netbox` and `documenso`. I read the source for what it flagged, which led to stricter rules for high severity, and it now raises no false high findings on those runs. To test that it catches a real problem, I added made-up drops of two columns in use to a throwaway copy of Mastodon's code, and it flagged both as `still-used`. It hasn't been tested in a fresh Claude session.

## Contributing

Contributions are welcome, from people and from people working with AI agents. See [CONTRIBUTING.md](CONTRIBUTING.md). Good first issues are [listed here](https://github.com/GillMacedo/stillused/issues?q=is%3Aissue+is%3Aopen+label%3A%22good+first+issue%22).

### How pull requests work

1. **Open a PR.** Fork the repo, work on a branch, and keep it to one change. Write a title that says what changes for users, because it becomes the changelog line: `feat:` goes under Added and `fix:` under Fixed.
2. **Automatic check.** Code changes need tests, and `make test` must pass. Changes over 30 lines need a linked open issue (`Closes #<number>`), and a PR can have at most 800 changed lines. If something is missing, a comment tells you what to fix.
3. **Review.** Claude reviews each PR automatically. Code PRs also get a security review and are tested in a sandbox. The review then merges the PR or asks for changes. PRs that change CI, agent settings, dependencies or build files go to the maintainer, as does anything the security review doesn't clear.
4. **Release.** A merge that changes what users install becomes a new version within about 20 minutes, with a GitHub Release and, if the project has one, its npm package. Docs and CI changes wait for the next release.

## AI disclosure

This project was largely generated with Claude Code and reviewed by its maintainer.

## About me

I'm Giulliano Macedo, and I build developer tools at GoTech Solutions. Follow along or get in touch:

- GitHub: [GillMacedo](https://github.com/GillMacedo)
- LinkedIn: [giulliano](https://www.linkedin.com/in/giulliano/)
- X: [@giumacedo](https://x.com/giumacedo)

If `stillused` stopped a bad migration, a star helps others find it.

---

Built by [Giulliano Macedo](https://github.com/GillMacedo), GoTech Solutions · [LinkedIn](https://www.linkedin.com/in/giulliano/) · [X](https://x.com/giumacedo)
