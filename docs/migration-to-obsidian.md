# Migrating to Obsidian

## 1. Run the export
```bash
quip-vault-exporter inventory
quip-vault-exporter export
quip-vault-exporter verify
```

## 2. Open the vault
Point Obsidian at `output_dir` (default `./exports/quip-vault`) via **Open folder as
vault**. Each document is a `.md` file with YAML frontmatter and resolved wikilinks.

## 3. How links resolve
A two-pass link map builds a complete `thread_id → title → vault path` index *before*
rendering, so internal Quip links become path-aliased wikilinks:
```
[[Clients/Client A/Onboarding|Onboarding]]
```
Path-aliased links keep working even when two documents share a title (the file is
disambiguated as `Title-<shortid>.md`, but the link still displays "Title"). Links to
documents outside the export set fall back to the original Quip URL, so nothing is silently
dropped.

## 4. Frontmatter
Every note carries `source`, `thread_id`, `aliases` (raw title + id), timestamps,
`original_url`, `folder_path`, and `parent_folder_ids` - so every file traces back to its
Quip source.

## 5. Comments
Comments export to `_comments/<title>.comments.md` (readable) and `.comments.json` (full
fidelity), paginated in full.

## 6. Before canceling Quip
Work through `_manifest/cancellation_checklist.md`. In particular: open a sample of notes in
Obsidian, confirm links/embeds resolve, keep the raw JSON, and copy the export offsite -
**all before your tenant reaches the Read-Only phase.**
