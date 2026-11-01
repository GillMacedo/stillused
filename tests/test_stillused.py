import contextlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "plugin" / "scripts"))
import stillused as s  # noqa: E402


def names(changes):
    return [(c.kind, c.table, c.name, c.new_name) for c in changes]


class ParseTest(unittest.TestCase):
    def test_sql_forms(self):
        sql = '''ALTER TABLE "Recipient" DROP COLUMN "documentId",
DROP COLUMN "templateId";
alter table if exists only public.users drop column if exists nick;
ALTER TABLE a RENAME COLUMN b TO c;
ALTER TABLE t RENAME x TO y;
ALTER TABLE old RENAME TO fresh;
DROP TABLE IF EXISTS one, two;
'''
        self.assertEqual(names(s.parse_sql(sql)), [
            ("drop-column", "Recipient", "documentId", ""), ("drop-column", "Recipient", "templateId", ""),
            ("drop-column", "users", "nick", ""), ("rename-column", "a", "b", "c"), ("rename-column", "t", "x", "y"),
            ("rename-table", "old", "old", "fresh"), ("drop-table", "one", "one", ""), ("drop-table", "two", "two", ""),
        ])

    def test_sql_ignores_constraints_and_comments(self):
        sql = "ALTER TABLE t DROP CONSTRAINT c;\nALTER TABLE t DROP COLUMN real_one; -- DROP COLUMN fake\n/* DROP TABLE nope; */\n"
        self.assertEqual(names(s.parse_sql(sql)), [("drop-column", "t", "real_one", "")])

    def test_drop_and_add_back_is_a_type_change(self):
        sql = "ALTER TABLE t DROP COLUMN kept, ADD COLUMN kept text;\nALTER TABLE t DROP COLUMN gone;"
        self.assertEqual(names(s.parse_sql(sql)), [("drop-column", "t", "gone", "")])

    def test_down_sections_are_ignored(self):
        goose = "-- +goose Up\nALTER TABLE t DROP COLUMN up_one;\n-- +goose Down\nALTER TABLE t DROP COLUMN down_one;\n"
        dbmate = "-- migrate:up\nDROP TABLE up_two;\n-- migrate:down\nDROP TABLE down_two;\n"
        self.assertEqual(names(s.parse_migration(goose, ".sql")), [("drop-column", "t", "up_one", "")])
        self.assertEqual(names(s.parse_migration(dbmate, ".sql")), [("drop-table", "up_two", "up_two", "")])

    def test_rails(self):
        text = "class X < ActiveRecord::Migration[7.0]\n  def up\n    remove_column :users, :nick, :string\n    remove_columns :users, :a, :b\n    rename_column :users, :old, :new\n    drop_table :logs\n    rename_table :x, :y\n  end\n  def down\n    remove_column :users, :back\n  end\nend\n"
        self.assertEqual(names(s.parse_migration(text, ".rb")), [
            ("drop-column", "users", "nick", ""), ("drop-column", "users", "string", ""), ("drop-column", "users", "a", ""), ("drop-column", "users", "b", ""),
            ("rename-column", "users", "old", "new"), ("drop-table", "logs", "logs", ""), ("rename-table", "x", "x", "y"),
        ][:1] + [("drop-column", "users", "a", ""), ("drop-column", "users", "b", ""), ("rename-column", "users", "old", "new"), ("drop-table", "logs", "logs", ""), ("rename-table", "x", "x", "y")])

    def test_django(self):
        text = "migrations.RemoveField(\n    model_name='order',\n    name='note',\n),\nmigrations.RenameField(model_name='order', old_name='a', new_name='b'),\nmigrations.DeleteModel(name='Coupon'),\nmigrations.RenameModel(old_name='X', new_name='Y'),\n"
        self.assertEqual(names(s.parse_migration(text, ".py")), [("drop-column", "order", "note", ""), ("rename-column", "order", "a", "b"), ("drop-table", "Coupon", "Coupon", ""), ("rename-table", "X", "X", "Y")])

    def test_alembic_and_downgrade(self):
        text = "def upgrade():\n    op.drop_column('t', 'c')\n    op.drop_table('u')\n    op.alter_column('t', 'a', new_column_name='b')\n    op.execute('ALTER TABLE v DROP COLUMN w')\n\ndef downgrade():\n    op.drop_column('t', 'added')\n"
        self.assertEqual(names(s.parse_migration(text, ".py")), [("drop-column", "t", "c", ""), ("drop-table", "u", "u", ""), ("rename-column", "t", "a", "b"), ("drop-column", "v", "w", "")])

    def test_knex_and_typeorm(self):
        text = "exports.up = (knex) => knex.schema.alterTable('t', (table) => {\n  table.dropColumn('a');\n  table.renameColumn('b', 'c');\n}).dropTableIfExists('old');\nexports.down = () => knex.schema.dropTable('back');\n"
        self.assertEqual(names(s.parse_migration(text, ".js")), [("drop-column", "t", "a", ""), ("rename-column", "t", "b", "c"), ("drop-table", "old", "old", "")])
        raw = 'await queryRunner.query(`ALTER TABLE "u" DROP COLUMN "v"`);\n'
        self.assertEqual(names(s.parse_migration(raw, ".ts")), [("drop-column", "u", "v", "")])


class HelperTest(unittest.TestCase):
    def test_spellings(self):
        self.assertEqual(s.spellings("template_id"), {"template_id", "templateId", "TemplateId"})
        self.assertEqual(s.spellings("templateId"), {"templateId", "template_id", "TemplateId"})

    def test_singular(self):
        self.assertEqual(s.singular("companies"), "company")
        self.assertEqual(s.singular("orders"), "order")

    def test_paths(self):
        for path in ("db/migrate/1_x.rb", "migrations/0001_x.sql", "app/migrations/0001_x.py", "alembic/versions/a.py", "packages/prisma/migrations/2025_x/migration.sql", "db/post_migrate/1_x.rb"):
            self.assertTrue(s.MIGRATION_PATH.search(path), path)
        for path in ("src/migrate.py", "app/models.py"):
            self.assertFalse(s.MIGRATION_PATH.search(path), path)


class ScanBase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def write(self, files):
        for name, text in files.items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)

    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = s.main([str(self.root), "--format", "json", *args])
        return code, json.loads(out.getvalue()), err.getvalue()

    def found(self, *args):
        code, report, _ = self.run_cli(*args)
        return code, {f["target"]: f for f in report["findings"]}, report


class ScanTest(ScanBase):
    def test_still_used_in_sql_and_camel_case(self):
        self.write({"migrations/1.sql": "ALTER TABLE orders DROP COLUMN discount_code;\nALTER TABLE orders DROP COLUMN gift_note;\n",
                    "app/a.py": 'db.run("SELECT discount_code FROM orders")\n', "app/b.ts": "const x = order.giftNote;\n"})
        code, found, _ = self.found()
        self.assertEqual(code, s.EXIT_FINDINGS)
        self.assertEqual(found["orders.discount_code"]["kind"], "still-used")
        self.assertEqual(found["orders.gift_note"]["kind"], "still-used")
        self.assertIn("app/a.py:1", found["orders.discount_code"]["places"])

    def test_name_away_from_its_table_is_only_possible(self):
        self.write({"migrations/1.sql": "ALTER TABLE orders DROP COLUMN gift_note;\n", "app/other.py": "gift_note = 1\n"})
        code, found, _ = self.found()
        self.assertEqual(found["orders.gift_note"]["kind"], "maybe-used")
        self.assertEqual(code, s.EXIT_OK)

    def test_common_names_need_their_table(self):
        self.write({"migrations/1.sql": "ALTER TABLE orders DROP COLUMN status;\n", "app/other.py": "status = 1\n", "app/orders_view.py": "x = 1\nstatus = 2\n"})
        code, found, report = self.found()
        self.assertEqual(found, {})
        self.assertEqual(report["clean"], ["orders.status (dropped)"])
        self.write({"app/q.py": 'db.run("SELECT status FROM orders")\n'})
        self.assertEqual(self.found()[1]["orders.status"]["kind"], "still-used")

    def test_another_model_on_the_line_isnt_sure(self):
        self.write({"migrations/1.sql": "ALTER TABLE accounts DROP COLUMN private_key;\n", "app/keypair.rb": "validates :private_key, presence: true, if: -> { account.local? }\n"})
        code, found, _ = self.found()
        self.assertEqual(found["accounts.private_key"]["kind"], "maybe-used")
        self.assertEqual(code, s.EXIT_OK)

    def test_javascript_import_isnt_sql(self):
        self.write({"migrations/1.sql": "DROP TABLE Document;\n", "app/a.ts": "import { Document } from './document';\n"})
        self.assertEqual(self.found()[1]["Document"]["kind"], "maybe-used")

    def test_lowercase_sql(self):
        self.write({"migrations/1.sql": "DROP TABLE coupons;\n", "app/a.py": 'db.run("select * from coupons")\n'})
        self.assertEqual(self.found()[1]["coupons"]["kind"], "still-used")

    def test_rails_ignored_columns_isnt_a_use(self):
        self.write({"migrations/1.sql": "ALTER TABLE accounts DROP COLUMN old_key;\n", "app/account.rb": "class Account\n  self.ignored_columns += %w(old_key)\nend\n"})
        self.assertEqual(self.found()[1], {})

    def test_table_named_like_an_ordinary_word_needs_sql(self):
        self.write({"migrations/1.sql": "DROP TABLE Document;\n", "app/a.tsx": "<Document />\n"})
        code, found, _ = self.found()
        self.assertEqual(found["Document"]["kind"], "maybe-used")
        self.assertEqual(code, s.EXIT_OK)

    def test_whole_words_only(self):
        self.write({"migrations/1.sql": "ALTER TABLE orders DROP COLUMN note;\n", "app/orders.py": "notes = 1\nkeynote = 2\n"})
        self.assertEqual(self.found()[1], {})

    def test_tables(self):
        self.write({"migrations/1.sql": "DROP TABLE coupons;\nDROP TABLE gone_table;\n", "app/a.py": 'db.run("SELECT * FROM coupons")\n'})
        code, found, report = self.found()
        self.assertEqual(found["coupons"]["kind"], "still-used")
        self.assertIn("gone_table (dropped)", report["clean"])

    def test_rename_looks_for_the_old_name(self):
        self.write({"migrations/1.sql": "ALTER TABLE orders RENAME COLUMN total TO total_cents;\n", "app/orders.py": "x = order.total_cents\n"})
        self.assertEqual(self.found()[1], {})
        self.write({"app/orders2.py": 'db.run("SELECT total FROM orders")\n'})
        self.assertIn("renamed to total_cents", self.found()[1]["orders.total"]["message"])

    def test_schema_file(self):
        self.write({"migrations/1.sql": "ALTER TABLE orders DROP COLUMN gift_note;\n", "prisma/schema.prisma": "model Order {\n  gift_note String\n}\n"})
        self.assertEqual(self.found()[1]["orders.gift_note"]["kind"], "schema-out-of-date")

    def test_tests_and_vendored_code_dont_count(self):
        self.write({"migrations/1.sql": "ALTER TABLE orders DROP COLUMN gift_note;\n", "tests/test_a.py": "orders gift_note\n", "vendor/a.py": "orders gift_note\n", "a_test.go": "orders gift_note\n"})
        self.assertEqual(self.found()[1], {})

    def test_older_migrations_dont_count(self):
        self.write({"migrations/1.sql": "ALTER TABLE orders DROP COLUMN old_one;\n", "migrations/2.sql": "ALTER TABLE orders ADD COLUMN old_one text;\n", "app/orders.py": "old_one = 1\n"})
        code, found, report = self.found("--last", "1")
        self.assertEqual(found, {})
        self.assertTrue(any("newest 1 of 2" in note for note in report["notes"]))
        self.assertIn("orders.old_one", self.found("--all")[1])

    def test_other_migrations_arent_code(self):
        self.write({"migrations/1.sql": "ALTER TABLE orders ADD COLUMN gift_note text;\n", "migrations/2.sql": "ALTER TABLE orders DROP COLUMN gift_note;\n"})
        self.assertEqual(self.found()[1], {})

    def test_named_file(self):
        self.write({"migrations/1.sql": "DROP TABLE aaa_table;\n", "migrations/2.sql": "DROP TABLE bbb_table;\n"})
        _, _, report = self.found("--file", "migrations/1.sql")
        self.assertEqual(report["clean"], ["aaa_table (dropped)"])

    def test_nothing_to_check_isnt_a_pass(self):
        self.write({"app/a.py": "x = 1\n"})
        code, _, err = self.run_cli()
        self.assertEqual(code, s.EXIT_FINDINGS)
        self.assertIn("nothing was checked", err)

    def test_fail_on(self):
        self.write({"migrations/1.sql": "ALTER TABLE orders DROP COLUMN gift_note;\n", "app/other.py": "gift_note = 1\n"})
        self.assertEqual(self.run_cli("--fail-on", "medium")[0], s.EXIT_FINDINGS)
        self.assertEqual(self.run_cli("--fail-on", "none")[0], s.EXIT_OK)

    def test_bad_input(self):
        for args in (["/no/such/place"], [str(self.root), "--last", "0"]):
            with self.assertRaises(SystemExit) as caught, contextlib.redirect_stderr(io.StringIO()):
                s.main(args)
            self.assertEqual(caught.exception.code, s.EXIT_USAGE)


class GitTest(ScanBase):
    def git(self, *args):
        env = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull)
        subprocess.run(["git", "-C", str(self.root), "-c", "user.name=D", "-c", "user.email=d@example.com", *args], check=True, capture_output=True, env=env)

    def setUp(self):
        super().setUp()
        self.git("init", "-q")
        self.write({"migrations/1.sql": "ALTER TABLE orders DROP COLUMN old_one;\n", "app/orders.py": 'db.run("SELECT gift_note FROM orders")\n'})
        self.git("add", ".")
        self.git("commit", "-qm", "Start")
        self.git("tag", "base")

    def test_since_checks_only_new_migrations(self):
        self.write({"migrations/2.sql": "DROP TABLE new_table;\n"})
        _, _, report = self.found("--since", "base")
        self.assertEqual(report["migrations"], ["migrations/2.sql"])

    def test_code_removed_in_the_same_change_is_a_deploy_order_risk(self):
        self.write({"migrations/2.sql": "ALTER TABLE orders DROP COLUMN gift_note;\n", "app/orders.py": 'db.run("SELECT id FROM orders")\n'})
        code, found, _ = self.found("--since", "base")
        finding = found["orders.gift_note"]
        self.assertEqual(finding["kind"], "deploy-order")
        self.assertEqual(finding["places"], ["app/orders.py"])
        self.assertEqual(code, s.EXIT_OK)
        self.assertEqual(self.run_cli("--since", "base", "--fail-on", "medium")[0], s.EXIT_FINDINGS)

    def test_no_new_migrations_is_fine(self):
        code, _, _ = self.run_cli("--since", "base")
        self.assertEqual(code, s.EXIT_OK)

    def test_bad_ref(self):
        with self.assertRaises(SystemExit) as caught, contextlib.redirect_stderr(io.StringIO()):
            s.main([str(self.root), "--since", "no-such-ref"])
        self.assertEqual(caught.exception.code, s.EXIT_USAGE)


class ExampleTest(unittest.TestCase):
    def test_example(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = s.main([str(ROOT / "examples" / "shop"), "--format", "json"])
        report = json.loads(out.getvalue())
        self.assertEqual(code, s.EXIT_FINDINGS)
        self.assertEqual({f["target"] for f in report["findings"]}, {"orders.discount_code", "orders.total", "coupons"})
        self.assertEqual(report["clean"], ["customers.legacy_ref (dropped)"])


class ProjectTest(unittest.TestCase):
    def test_standard_library_and_no_database(self):
        source = (ROOT / "plugin" / "scripts" / "stillused.py").read_text()
        imported = set(re.findall(r"^(?:from|import) (\w+)", source, re.M))
        self.assertLessEqual(imported, {"argparse", "json", "os", "re", "subprocess", "sys", "dataclasses", "pathlib"})

    def test_skill_frontmatter(self):
        text = (ROOT / "plugin" / "skills" / "checking-schema-drops" / "SKILL.md").read_text()
        self.assertRegex(re.search(r"^name: (.+)$", text, re.M).group(1), r"^[a-z0-9-]{1,64}$")
        self.assertLessEqual(len(re.search(r"^description: (.+)$", text, re.M).group(1)), 1024)

    def test_evals_are_valid(self):
        self.assertGreaterEqual(len(json.loads((ROOT / "evals" / "evals.json").read_text())), 3)


if __name__ == "__main__":
    unittest.main()
