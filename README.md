# University Press Citation Review Workbench

This Tech Triangle workbench turns a folder of chapter manuscripts into an Explorer-friendly set of author review packets. It preserves every source file, runs the pinned Reference and Citation Integrity package before the pinned Scholarly and Editorial Integrity package, and records proposed changes, queries, evidence limits, and release conditions.

The workbench is a review and routing surface. It does not accept edits, adjudicate integrity concerns, overwrite a source, publish a manuscript, or claim that provider metadata proves source identity.

## Supervised private chapter review

This separate candidate opens with a local, single-file review. Inspect the selected manuscript hash, enter the edition and case owner, then build a Word copy, evidence ledger and decision ledger. The offline workflow never requests a provider or metadata lookup. See [the private run guide](PRIVATE-REVIEW.md) for desktop and command-line instructions, limits and recovery.

## Provider-assisted batch workbench

1. Install Python 3.11 or newer and run `python -m pip install -r requirements.txt`. Codex users can skip this because the launcher finds the bundled Python runtime.
2. For the provider-assisted batch lane, confirm that `LUNAROUTE_API_KEY` is stored as a Windows user environment variable. Never paste the key into this repository, a chapter folder, or a command you intend to save.
3. For a separately authorized provider session, run `.\Start-Workbench.ps1 -EnableProvider`. The default desktop session only builds offline packets.
4. The workbench opens at `http://127.0.0.1:8765/`.
5. Expand **Provider-assisted batch review**, then enter the chapter folder and a separate output parent folder.
6. Scan the source set, name the editor or case owner, choose the classification, and affirm that LunaRoute is approved for the complete manifest.
7. Start the run. Keep the PowerShell window open until the run finishes.
8. Choose **Open output folder**. Start with `00_OPEN_RUN_SUMMARY.html`, then open each chapter's `author-review` folder.

Supported chapter formats are `.docx`, `.md`, and UTF-8 `.txt`. Temporary Word owner files, symlinks, reparse points, empty files, and unsupported formats are skipped. Files larger than 20 MB and batches larger than 250 chapters or 500 MB are rejected before model processing.

## Output structure

Each run creates a new folder; it never writes into the chapter folder.

```text
UniversityPress-Review-<timestamp>-<id>/
  00_SOURCE_MANIFEST.json
  00_IMMUTABLE_PLAN.json
  00_COMPANY_CONSTITUTION.json
  00_WORKBENCH_CONFIGURATION.json
  00_RUN_RECEIPT.json
  00_OPEN_RUN_SUMMARY.html
  events.jsonl
  learning-observations.jsonl
  chapters/
    CH001-<chapter-name>/
      author-review/
        <chapter>__AUTHOR_REVIEW.docx
        <chapter>__AUTHOR_QUERIES.md
        OPEN_REVIEW.html
      editorial-record/
        citation-inventory.json
        rci-run-result.json
        rci-workbench-record.json
        sei-run-result.json
        sei-workbench-record.json
        chapter-receipt.json
```

The reviewed Word copy contains comments where a finding can be anchored safely and always includes a visible review appendix. The appendix is the fallback when Word comment insertion is unavailable. Proposed edits remain proposals.

## Batch command

For repeatable folder runs, use:

```powershell
.\Run-Batch.ps1 -InputFolder 'C:\Press\Volume 1\Chapters' -OutputFolder 'C:\Press\Volume 1\Reviews' -CaseOwner 'Volume editor' -ApproveLunaRoute
```

`-ApproveLunaRoute` is intentionally required on every run. The approval is bound to the source manifest hash, provider, classification, and timestamp.

## Review lanes

`glm-5.3-background` is the default for ordinary chapter batches. Use `glm-5.3` only when latency matters and the foreground lane is justified. The workbench permits at most six parallel chapters and applies a hard two-model-call budget per chapter.

## Important limits

- The two skill packages are draft v0.1.0 policies. Their unresolved decision hooks remain visible and keep many release checks at `ready_with_conditions` or `not_ready`.
- Crossref records are labeled candidate metadata. Arbitrary manuscript URLs are never fetched.
- A source-unavailable result cannot be promoted to verified by model confidence.
- The deterministic citation parser is deliberately conservative. Unusual styles or reference lists without a recognized heading receive partial coverage and human review.
- Encrypted files, active content, embedded objects, unsafe external relationships, symlinks, and reparse points are blocked. Flatten embedded objects into ordinary document content before review.
- Only a named press editor or volume owner can accept changes or authorize publication.

See `DESIGN.md` for the architecture, `RECENT_CODEX_CONTEXT.md` for the decisions carried forward from the last week's work, `PACKAGE_AUDIT.md` for the two-skill review, and `VERIFICATION.md` for test evidence and pilot conditions.
