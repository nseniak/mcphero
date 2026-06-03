"""One-shot data migration scripts.

Each script ships with the code change that requires it. After a
script has run in prod and its companion code change is deployed,
the script stays here as a record (no auto-pruning) so an operator
running through the deploy log later can find it.

Each script must:
- Document its backup story in its module docstring (typically
  ``mongodump`` against the affected collection).
- Honor ``MIGRATIONS_DRY_RUN=true`` to log intended changes without
  writing.
- Print a summary of action counts to stdout/structlog at the end.
"""
