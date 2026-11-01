# 0001: Compare text, with no database connection

**Decision:** read migrations and code as text, and never connect to a database.

**Why:** it can run on any pull request with no credentials, and it can't harm data. The question it answers, whether code still uses a name, doesn't need the database.

**Alternatives:** type-aware analysis for each ORM would be more exact, but needs a separate tool per language and a buildable project. A database connection could confirm that a column exists, but adds credentials and risk.
