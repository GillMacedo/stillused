#!/usr/bin/env python3
"""Find columns and tables that migrations drop or rename while code still uses them. Reads files only."""

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

EXIT_OK, EXIT_FINDINGS, EXIT_USAGE = 0, 1, 2
DEFAULT_LAST = 5  # older migrations have shipped already, and their names are often reused
MAX_FILE_BYTES = 2_000_000  # larger files are almost always generated or data
PLACES_SHOWN = 5
GIT_TIMEOUT = 60  # seconds
SKIP_DIRS = {".git", "node_modules", "vendor", "dist", "build", "target", ".next", "__pycache__", ".venv", "venv", "coverage", "third_party", "testdata"}
TEST_PATH = re.compile(r"(_test\.go$|\.test\.[jt]sx?$|\.spec\.[jt]sx?$|_spec\.rb$|_test\.rb$|(^|/)test_[^/]+\.py$|_test\.py$|(^|/)(tests?|__tests__|spec|e2e|fixtures)/)")
MIGRATION_PATH = re.compile(r"(^|/)(migrations?|migrate|post_migrate|alembic/versions|db/migrate)/[^/]*\.(sql|rb|py|ts|js)$|(^|/)migrations?/[^/]+/[^/]*\.sql$")
DOWN_FILE = re.compile(r"(\.down\.sql|_down\.sql|/down\.sql)$")
SCHEMA_FILE = re.compile(r"(^|/)(schema\.prisma|schema\.rb|structure\.sql|schema\.sql)$")
CODE_SUFFIXES = {".go", ".py", ".rb", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".java", ".kt", ".rs", ".php", ".cs", ".sql", ".prisma", ".graphql", ".erb", ".vue", ".svelte"}
# These appear everywhere, so a match means something only next to the table's name or in SQL.
COMMON_NAMES = {"id", "name", "type", "status", "value", "data", "key", "title", "state", "url", "email", "description", "created_at", "updated_at", "deleted_at", "createdat", "updatedat", "user_id", "userid", "code", "active", "enabled", "count", "version", "position", "label", "content", "body", "token"}
# Uppercase, or a lowercase select with its from, because "from" alone is also a JavaScript import.
SQL_WORDS = re.compile(r"\b(SELECT|INSERT\s+INTO|UPDATE|DELETE\s+FROM|WHERE|JOIN|FROM|ORDER\s+BY|GROUP\s+BY)\b|\bselect\b.*\bfrom\b|\binsert\s+into\b|\bupdate\s+\w+\s+set\b|\bdelete\s+from\b")
# Lines that exist to let a column go, such as Rails' ignored_columns, aren't uses of it.
SAFE_REMOVAL = re.compile(r"ignored_columns|ignore_column|@Ignore|remove_column|drop_column")
IDENT = r'[`"\[]?(\w+)[`"\]]?'
QUALIFIED = rf'(?:{IDENT}\.)?{IDENT}'


@dataclass
class Change:
    kind: str  # drop-column, rename-column, drop-table or rename-table
    table: str
    name: str
    new_name: str = ""
    migration: str = ""


@dataclass
class Finding:
    severity: str
    kind: str
    target: str
    migration: str
    message: str
    places: list = field(default_factory=list)
    total: int = 0


@dataclass
class Report:
    root: str
    migrations: list = field(default_factory=list)
    changes: int = 0
    code_files: int = 0
    findings: list = field(default_factory=list)
    clean: list = field(default_factory=list)
    notes: list = field(default_factory=list)


def up_part(text, suffix):
    """Keep only what runs going forward, because a down migration drops what the up one added."""
    if suffix == ".sql":
        match = re.search(r"(?im)^\s*--\s*(\+goose\s+down|\+migrate\s+down|migrate:down|\+\s*down)\b", text)
        return text[: match.start()] if match else text
    if suffix == ".rb":
        match = re.search(r"(?m)^\s*def\s+(self\.)?down\b", text)
        return text[: match.start()] if match else text
    if suffix == ".py":
        match = re.search(r"(?m)^\s*def\s+downgrade\b", text)
        return text[: match.start()] if match else text
    match = re.search(r"(?m)^\s*(export\s+(async\s+)?function\s+down\b|exports\.down\b|export\s+const\s+down\b|(public\s+)?async\s+down\s*\()", text)
    return text[: match.start()] if match else text


def strip_sql_comments(text):
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.DOTALL)
    return re.sub(r"--[^\n]*", " ", text)


def parse_sql(text):
    changes, added = [], set()
    for statement in strip_sql_comments(text).split(";"):
        statement = statement.strip()
        match = re.match(rf"(?is)^DROP\s+TABLE\s+(?:IF\s+EXISTS\s+)?(.+)$", statement)
        if match:
            for part in match.group(1).split(","):
                found = re.match(rf"\s*{QUALIFIED}", part)
                if found:
                    changes.append(Change("drop-table", found.group(2), found.group(2)))
            continue
        match = re.match(rf"(?is)^ALTER\s+TABLE\s+(?:IF\s+EXISTS\s+)?(?:ONLY\s+)?{QUALIFIED}\s+(.+)$", statement)
        if not match:
            continue
        table, actions = match.group(2), match.group(3)
        renamed = re.match(rf"(?is)^RENAME\s+TO\s+{IDENT}", actions)
        if renamed:
            changes.append(Change("rename-table", table, table, renamed.group(1)))
            continue
        for found in re.finditer(rf"(?i)\bDROP\s+(?:COLUMN\s+)(?:IF\s+EXISTS\s+)?{IDENT}", actions):
            changes.append(Change("drop-column", table, found.group(1)))
        for found in re.finditer(rf"(?i)\bRENAME\s+(?:COLUMN\s+)?{IDENT}\s+TO\s+{IDENT}", actions):
            changes.append(Change("rename-column", table, found.group(1), found.group(2)))
        for found in re.finditer(rf"(?i)\bADD\s+(?:COLUMN\s+)?(?:IF\s+NOT\s+EXISTS\s+)?{IDENT}", actions):
            added.add((table.lower(), found.group(1).lower()))
    # Dropped and added back in one file is a type change, and the name stays in use.
    return [c for c in changes if not (c.kind == "drop-column" and (c.table.lower(), c.name.lower()) in added)]


def symbol_args(text):
    return re.findall(r"""[:"']([\w?!]+)["']?""", text)


def parse_rails(text):
    changes = []
    for match in re.finditer(r"(?m)^\s*(?:safety_assured\s*\{\s*)?(remove_columns?|rename_column|drop_table|rename_table)\b\s*\(?([^\n#]*)", text):
        verb, args = match.group(1), symbol_args(match.group(2).split("if_exists")[0].split(", type:")[0])
        if verb == "remove_column" and len(args) >= 2:
            changes.append(Change("drop-column", args[0], args[1]))
        elif verb == "remove_columns" and len(args) >= 2:
            changes.extend(Change("drop-column", args[0], name) for name in args[1:])
        elif verb == "rename_column" and len(args) >= 3:
            changes.append(Change("rename-column", args[0], args[1], args[2]))
        elif verb == "drop_table" and args:
            changes.append(Change("drop-table", args[0], args[0]))
        elif verb == "rename_table" and len(args) >= 2:
            changes.append(Change("rename-table", args[0], args[0], args[1]))
    return changes


def parse_python(text):
    changes = []
    quoted = r"""["'](\w+)["']"""
    for match in re.finditer(rf"RemoveField\(\s*model_name\s*=\s*{quoted}\s*,\s*name\s*=\s*{quoted}", text):
        changes.append(Change("drop-column", match.group(1), match.group(2)))
    for match in re.finditer(rf"RenameField\(\s*model_name\s*=\s*{quoted}\s*,\s*old_name\s*=\s*{quoted}\s*,\s*new_name\s*=\s*{quoted}", text):
        changes.append(Change("rename-column", match.group(1), match.group(2), match.group(3)))
    for match in re.finditer(rf"DeleteModel\(\s*name\s*=\s*{quoted}", text):
        changes.append(Change("drop-table", match.group(1), match.group(1)))
    for match in re.finditer(rf"RenameModel\(\s*old_name\s*=\s*{quoted}\s*,\s*new_name\s*=\s*{quoted}", text):
        changes.append(Change("rename-table", match.group(1), match.group(1), match.group(2)))
    for match in re.finditer(rf"op\.drop_column\(\s*{quoted}\s*,\s*{quoted}", text):
        changes.append(Change("drop-column", match.group(1), match.group(2)))
    for match in re.finditer(rf"op\.drop_table\(\s*{quoted}", text):
        changes.append(Change("drop-table", match.group(1), match.group(1)))
    for match in re.finditer(rf"op\.rename_table\(\s*{quoted}\s*,\s*{quoted}", text):
        changes.append(Change("rename-table", match.group(1), match.group(1), match.group(2)))
    for match in re.finditer(rf"op\.alter_column\(\s*{quoted}\s*,\s*{quoted}[^)]*new_column_name\s*=\s*{quoted}", text):
        changes.append(Change("rename-column", match.group(1), match.group(2), match.group(3)))
    # SQL inside op.execute or RunSQL.
    for block in re.findall(r'(?:op\.execute|RunSQL)\(\s*(?:f|r)?("""|\'\'\'|"|\')(.*?)\1', text, flags=re.DOTALL):
        changes.extend(parse_sql(block[1]))
    return changes


def parse_js(text):
    changes = []
    quoted = r"""["'`](\w+)["'`]"""
    for block in re.finditer(rf"(?:alterTable|table)\(\s*{quoted}\s*,", text):
        table = block.group(1)
        body = text[block.end() : block.end() + 4000]
        end = re.search(r"\n\s*\}\s*\)", body)
        body = body[: end.start()] if end else body
        for match in re.finditer(rf"\.dropColumn\(\s*{quoted}", body):
            changes.append(Change("drop-column", table, match.group(1)))
        for match in re.finditer(rf"\.renameColumn\(\s*{quoted}\s*,\s*{quoted}", body):
            changes.append(Change("rename-column", table, match.group(1), match.group(2)))
    for match in re.finditer(rf"\.dropTable(?:IfExists)?\(\s*{quoted}", text):
        changes.append(Change("drop-table", match.group(1), match.group(1)))
    for match in re.finditer(rf"\.renameTable\(\s*{quoted}\s*,\s*{quoted}", text):
        changes.append(Change("rename-table", match.group(1), match.group(1), match.group(2)))
    # SQL strings, as in TypeORM and raw knex calls.
    for match in re.finditer(r"""(?:query|raw|execute|sql)\(\s*(`[^`]+`|"[^"]+"|'[^']+')""", text):
        changes.extend(parse_sql(match.group(1)[1:-1]))
    return changes


def parse_migration(text, suffix):
    text = up_part(text, suffix)
    if suffix == ".sql":
        return parse_sql(text)
    if suffix == ".rb":
        return parse_rails(text)
    if suffix == ".py":
        return parse_python(text)
    return parse_js(text)


def spellings(name):
    """snake_case and camelCase forms, since ORMs often map one to the other."""
    words = [word for word in re.split(r"_|(?<=[a-z0-9])(?=[A-Z])", name) if word]
    if not words:
        return {name}
    lower = [word.lower() for word in words]
    return {name, "_".join(lower), lower[0] + "".join(word.capitalize() for word in lower[1:]), "".join(word.capitalize() for word in lower)}


def singular(name):
    lower = name.lower()
    if lower.endswith("ies"):
        return lower[:-3] + "y"
    if lower.endswith("ses"):
        return lower[:-2]
    return lower[:-1] if lower.endswith("s") else lower


def word_pattern(names):
    return re.compile(r"(?<![\w])(" + "|".join(sorted(map(re.escape, names), key=len, reverse=True)) + r")(?![\w])")


def direct_access(line, table, spell):
    """True for order.gift_note, order?.giftNote or orders["gift_note"], where the table owns the name."""
    owners = {table.lower(), singular(table)}
    pattern = r"(?i)\b(" + "|".join(map(re.escape, owners)) + r")(?:\?\.|\.|->|\[[\"']|_)(" + "|".join(map(re.escape, spell)) + r")\b"
    return bool(re.search(pattern, line))


def table_mentioned(text, table):
    lower = text.lower().replace("_", "")
    bare = table.lower().replace("_", "")
    return bare in lower or singular(table).replace("_", "") in lower


def walk(root):
    for folder, subfolders, files in os.walk(root):
        subfolders[:] = [name for name in subfolders if name not in SKIP_DIRS and not name.startswith(".")]
        for name in sorted(files):
            path = Path(folder, name)
            yield path, path.relative_to(root).as_posix()


def read(path):
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            return None
        return path.read_text(errors="replace")
    except OSError:
        return None


def git(root, *args):
    try:
        done = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, errors="replace", timeout=GIT_TIMEOUT, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return done.stdout if done.returncode == 0 else None


def choose_migrations(root, args, report):
    found = [relative for path, relative in walk(root) if MIGRATION_PATH.search(relative) and not DOWN_FILE.search(relative)]
    if args.file:
        return args.file
    if args.since:
        changed = git(root, "diff", "--name-only", args.since, "--")
        untracked = git(root, "ls-files", "--others", "--exclude-standard")
        if changed is None:
            raise ValueError(f"git couldn't compare with {args.since}")
        names = set(changed.splitlines()) | set((untracked or "").splitlines())
        return sorted(name for name in names if MIGRATION_PATH.search(name) and not DOWN_FILE.search(name) and (root / name).exists())
    found.sort(key=lambda name: (name.rsplit("/", 2)[-2] if "/" in name and name.endswith("migration.sql") else "", Path(name).name))
    if args.all:
        return found
    if len(found) > args.last:
        report.notes.append(f"Checked the newest {args.last} of {len(found)} migrations. Use --last, --all or --since for others.")
    return found[-args.last :]


def removed_in_diff(root, base, spell):
    """Non-migration lines that used a name and were removed since base, which means old code still uses it."""
    diff = git(root, "diff", "--unified=0", base, "--")
    if not diff:
        return []
    pattern = word_pattern(spell)
    current, places = "", []
    for line in diff.splitlines():
        if line.startswith("+++ b/"):
            current = line[6:]
        elif line.startswith("--- a/"):
            current = line[6:]
        elif line.startswith("-") and not line.startswith("---") and pattern.search(line):
            if not MIGRATION_PATH.search(current) and not SCHEMA_FILE.search(current) and not TEST_PATH.search(current):
                places.append(current)
    return sorted(set(places))


def search(code, change, spell):
    """Return (likely, possible, schema) places for a name, scored by what's around each match."""
    pattern = word_pattern(spell)
    common = change.name.lower() in COMMON_NAMES
    likely, possible, schema = [], [], []
    is_table = change.kind.endswith("table")
    for relative, text in code:
        if not pattern.search(text):
            continue
        in_file = is_table or table_mentioned(text, change.table)
        for number, line in enumerate(text.splitlines(), 1):
            if not pattern.search(line):
                continue
            if SAFE_REMOVAL.search(line):
                continue
            place = f"{relative}:{number}"
            if SCHEMA_FILE.search(relative):
                schema.append(place)
            elif SQL_WORDS.search(line) and (is_table or table_mentioned(line, change.table)):
                likely.append(place)
            elif not is_table and direct_access(line, change.table, spell):
                likely.append(place)
            elif common and not table_mentioned(line, change.table):
                continue
            elif is_table or in_file or not common:
                possible.append(place)
    return likely, possible, schema


def describe(change):
    what = "table" if change.kind.endswith("table") else "column"
    target = change.name if what == "table" else f"{change.table}.{change.name}"
    if change.new_name:
        return what, target, f"renamed to {change.new_name}"
    return what, target, "dropped"


def scan(root, args):
    report = Report(root=str(root))
    chosen = choose_migrations(root, args, report)
    report.migrations = chosen
    changes = []
    for relative in chosen:
        path = root / relative
        text = read(path)
        if text is None:
            report.notes.append(f"Couldn't read {relative}.")
            continue
        for change in parse_migration(text, path.suffix.lower()):
            change.migration = relative
            changes.append(change)
    report.changes = len(changes)
    chosen_set = set(chosen)
    code = []
    for path, relative in walk(root):
        if path.suffix.lower() not in CODE_SUFFIXES or relative in chosen_set or MIGRATION_PATH.search(relative) or TEST_PATH.search(relative):
            continue
        text = read(path)
        if text is not None:
            code.append((relative, text))
    report.code_files = len(code)
    for change in changes:
        what, target, verb = describe(change)
        spell = spellings(change.name)
        likely, possible, schema = search(code, change, spell)
        places = likely or possible
        if likely:
            severity, kind = "HIGH", "still-used"
            message = f"This {what} is {verb}, and code still uses the old name."
        elif possible:
            severity, kind = "MEDIUM", "maybe-used"
            message = f"This {what} is {verb}. The old name appears in code, but the tool can't tell that it's this {what}, so check each one."
        elif schema:
            severity, kind, places = "MEDIUM", "schema-out-of-date", schema
            message = f"This {what} is {verb}, and only a schema file still has the old name."
        else:
            old_users = removed_in_diff(root, args.since, spell) if args.since else []
            if old_users:
                report.findings.append(Finding("MEDIUM", "deploy-order", target, change.migration, f"This {what} is {verb}, and this same change removed its use from code. Pods running the old code still use it during a rollout, so ship the code first and the migration in a later release.", old_users[:PLACES_SHOWN], len(old_users)))
            else:
                report.clean.append(f"{target} ({verb})")
            continue
        report.findings.append(Finding(severity, kind, target, change.migration, message, places[:PLACES_SHOWN], len(places)))
    order = {"HIGH": 0, "MEDIUM": 1}
    report.findings.sort(key=lambda finding: (order[finding.severity], finding.target))
    if not chosen:
        report.notes.append("No migration files found to check.")
    return report


def render_text(report):
    lines = [f"{report.changes} drops and renames in {len(report.migrations)} migrations, compared with {report.code_files} code files"]
    for finding in report.findings:
        lines += ["", f"{finding.severity:<6} {finding.kind}: {finding.target}", f"  {finding.migration}", f"  {finding.message}"]
        lines.extend(f"    {place}" for place in finding.places)
        if finding.total > len(finding.places):
            lines.append(f"    ...and {finding.total - len(finding.places)} more")
    if report.clean:
        lines += ["", "No references found: " + ", ".join(report.clean)]
    for note in report.notes:
        lines += ["", note]
    lines += ["", "This compares names in text, without a database. Code built at runtime and other services that share the database aren't visible."]
    return "\n".join(lines)


def render_markdown(report):
    lines = [f"## stillused: {len(report.findings)} findings", "", "| Severity | Kind | Target | Migration | Used at |", "|---|---|---|---|---|"]
    for f in report.findings:
        lines.append(f"| {f.severity} | {f.kind} | `{f.target}` | `{f.migration}` | {', '.join(f'`{p}`' for p in f.places)} |")
    if report.clean:
        lines += ["", "No references found: " + ", ".join(report.clean)]
    for note in report.notes:
        lines += ["", note]
    return "\n".join(lines)


def render_json(report):
    return json.dumps(asdict(report), indent=2)


RENDERERS = {"text": render_text, "markdown": render_markdown, "json": render_json}
FAIL_ON = {"high": {"HIGH"}, "medium": {"HIGH", "MEDIUM"}, "none": set()}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Find columns and tables that migrations drop or rename while code still uses them.")
    parser.add_argument("path", nargs="?", default=".", help="repo to scan (default: current folder)")
    parser.add_argument("--since", metavar="REF", help="check migrations changed since this git ref, and flag deploy order risks")
    parser.add_argument("--last", type=int, default=DEFAULT_LAST, help=f"check the newest N migrations (default {DEFAULT_LAST})")
    parser.add_argument("--all", action="store_true", help="check every migration")
    parser.add_argument("--file", action="append", metavar="PATH", help="check this migration, relative to the repo; repeatable")
    parser.add_argument("--format", choices=sorted(RENDERERS), default="text")
    parser.add_argument("--fail-on", choices=sorted(FAIL_ON), default="high", help="exit 1 at this severity or above (default high)")
    args = parser.parse_args(argv)
    root = Path(args.path).resolve()
    if not root.is_dir():
        parser.error(f"{args.path} isn't a folder")
    if args.last < 1:
        parser.error("--last must be at least 1")
    try:
        report = scan(root, args)
    except ValueError as error:
        parser.error(str(error))
    print(RENDERERS[args.format](report))
    if not report.migrations:
        print("stillused: no migrations found, so nothing was checked", file=sys.stderr)
        return EXIT_FINDINGS if not args.since else EXIT_OK
    return EXIT_FINDINGS if any(finding.severity in FAIL_ON[args.fail_on] for finding in report.findings) else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
