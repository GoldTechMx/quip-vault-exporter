"""Quip Vault Exporter - read-only export of a Quip workspace to an Obsidian vault.

See CLAUDE.md for the validated spec and ../VALIDATION_REPORT.md for the analysis that
shaped this design.
"""

__version__ = "0.1.0"

# The minimum text the README and CLI banner must surface - Quip is being retired.
EOL_NOTICE = (
    "Quip is being retired by Salesforce (EOL announced 2026-03-04; no renewals after "
    "2027-03-01; then Read-Only -> Blocked -> Deletion). The Automation/Admin API stops "
    "at the Read-Only phase. Finish your export before your tenant's subscription lapses."
)
