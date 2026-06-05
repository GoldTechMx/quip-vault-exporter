"""Export orchestration: the resilient, structure-preserving pipeline.

    PHASE 1  SCAN      (API)    build + persist Quip's folder/sub-folder tree
    PHASE 2  DOWNLOAD  (API)    fetch every document once into the raw store (resumable)
    PHASE 3  LINK MAP  (local)  resolve paths + wikilinks
    PHASE 4  PROCESS   (local)  render the ordered Obsidian vault from the raw store

Phases 1-2 are the only rate-limited part. Phases 3-4 never touch the API, so they are fast
and re-runnable. State is persisted per document, so a crashed/cancelled run resumes.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .cache import RawStore
from .config import Config, validate_output_dir
from .control import Control
from .downloader import Downloader, DownloadStats
from .inventory import Inventory
from .linkmap import LinkMap
from .processor import Processor, ProcessStats
from .quip_client import QuipClient
from .state import StateDB
from .utils import ensure_dir

Progress = Any  # Callable[[str, int, int | None, str], None] | None


@dataclass
class ExportRun:
    inventory: Inventory
    download: DownloadStats
    process: ProcessStats

    def summary(self) -> dict[str, int]:
        return {
            "documents": len(self.inventory.threads),
            "folders": len(self.inventory.folders),
            "downloaded": self.download.downloaded,
            "reused": self.download.reused,
            "rendered": self.process.processed,
            "failed": self.download.failed + self.process.failed,
            "assets": self.download.assets,
            "comments_threads": self.download.comments_threads,
            "pdfs": self.process.pdfs,
            "spreadsheets": self.process.spreadsheets,
        }


def run_export(
    *,
    config: Config,
    client: QuipClient,
    state: StateDB,
    secrets: list[str],
    raw: RawStore,
    force: bool = False,
    progress: Progress = None,
    control: Control | None = None,
) -> ExportRun:
    validate_output_dir(config.output_dir)  # fail fast on a Windows path inside a Linux container
    mdir = ensure_dir(Path(config.output_dir) / config.obsidian.manifest_folder_name)

    inv = Inventory(config, client)
    inv.scan(progress=progress, control=control)
    inv.hydrate_from_state(state)  # restore titles for resume/incremental (before persist)
    inv.persist(state)  # structure is durable here (resumable from this point)
    inv.write_manifest(mdir)

    downloader = Downloader(config, client, state, raw, secrets)
    downloader.download_all(inv, force=force, progress=progress, control=control)

    linkmap = LinkMap(config)
    linkmap.build(inv, state)

    proc = Processor(config, state, linkmap, raw, secrets)
    proc.process_all(inv, force=force, progress=progress, control=control)

    return ExportRun(inventory=inv, download=downloader.stats, process=proc.stats)
