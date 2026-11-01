# VERSIONING — daily-memory-archiver (DMA)

Rules for bumping DMA's versions. **Written for an AI maintainer: follow
exactly.** This file exists because the version number scattered across
`SKILL.md` prose and `README.md` and drifted out of sync. The fix is a single
source of truth per axis + these rules + a release check.

## Three independent version axes — do NOT conflate them
DMA carries **three** version numbers, each tracking a different contract.
Treating them as one is exactly what caused the past mess.

### 1. `skill_version` — SemVer of the skill's behavior
- **Single source of truth:** `SKILL.md` frontmatter (`skill_version:`).
  README and any prose must **match** it, never independently define it.
- **MAJOR** — a breaking change to the output format or to behavior a
  consumer / cron relies on.
- **MINOR** — a backward-compatible feature (e.g. W2 emitting the
  `### 结构化事实` block: additive, old consumers ignore it).
- **PATCH** — a bug fix or prompt tweak with no behavior-contract change.

### 2. `config_version` — integer, `config.yaml` schema version
- **Single source of truth:** `config/config.yaml` (`config_version:`),
  mirrored in `SKILL.md` frontmatter.
- Bump **only** when `config.yaml`'s schema changes in a way that needs
  migration (added / renamed / removed keys, or changed meaning of a key).
- **Do not** bump for env-var-only features: e.g. `DAILY_MEMORY_LEXICON_CMD` is
  an environment variable, **not** a `config.yaml` key → no bump.

### 3. `spec_version` — the `KW_MEMORY_FILE_SPEC` DMA writes to
- Tracks the memory-file contract DMA's output conforms to. See the shared
  spec-coupling section below.

## On every release
1. Bump the relevant axis/axes in `SKILL.md` frontmatter (the source); sync
   `config/config.yaml` if `config_version` changed.
2. Update any `README.md` / SKILL body references to match (or have them read
   from frontmatter) — never leave a second number behind.
3. Run the consistency check below.
4. `git tag` the release and push the tag.

## Spec coupling — KW ↔ DMA  (keep this section byte-identical in both repos)
KW reads the memory files DMA writes. The contract between the two is the
**`KW_MEMORY_FILE_SPEC` version** plus the W2 `### 结构化事实` JSON-facts schema.
The package version numbers do **not** indicate compatibility — the spec does.
- When the memory-file format or the structured-facts schema changes, bump the
  **`KW_MEMORY_FILE_SPEC`** version.
- In the **same coordinated change**, update the "implements spec vX" declaration
  in **both** repos (KW's parser side and DMA's writer side).
- A KW release and a DMA release are compatible **iff they declare the same
  `KW_MEMORY_FILE_SPEC` major**. Never assume the package numbers imply it.

## Release consistency check (gives the rules teeth)
Before tagging, assert all of:
- `skill_version` in `SKILL.md` frontmatter **==** the git tag being created.
- `config_version` in `SKILL.md` frontmatter **==** `config/config.yaml`.
- No prose (`README.md`, SKILL body) hardcodes a number different from the
  frontmatter source (`grep`).
- The `spec_version` DMA declares **==** the spec its output actually conforms to.
