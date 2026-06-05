"""Enable `python -m quip_vault_exporter ...` as an alias for the console script.

Handy when the installed `quip-vault-exporter` script is not on PATH (a common Windows
situation where Python's Scripts directory is not added to PATH).
"""

from .cli import main

if __name__ == "__main__":
    main()
