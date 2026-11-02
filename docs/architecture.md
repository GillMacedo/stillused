# Architecture

    choose_migrations --> parse_migration (up direction only) --> changes
    search --> each use of an old name, scored as likely, possible or schema
    removed_in_diff --> with --since, code removed in the same change
    render_* --> text, Markdown or JSON

- **High severity** needs SQL naming the table, including across a few nearby lines, or direct access like `order.gift_note`.
- **Spellings** cover `snake_case`, `camelCase` and `PascalCase`, since ORMs map between them.
- **Older migrations** are left out by default, because old names are often reused.

Key choice: compare text with no database connection. See `decisions/`.
