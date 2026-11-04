# Supervised private review

Choose one manuscript file and identify the edition or draft date yourself. Store the source and outputs in separate, private local folders outside the application, Git and coauthor-accessible storage. On Windows use a current CPython runtime on a filesystem with private access controls. The tested runtime is CPython 3.12.14 on NTFS.

## Use the desktop workbench

1. Install the runtime dependency with `python -m pip install -r requirements.txt` or use the bundled Codex Python with python-docx 1.2.0. From the application folder run `.\Start-Workbench.ps1`.
2. In **Review one chapter offline**, enter the full path to the source and choose **Inspect selected file**. Compare the displayed SHA-256 with the edition you intended to select.
3. Enter the draft identifier, editor or case owner, and an existing output folder. Select **Build offline Word packet**. Each run produces a separate folder. The private workflow runs without a key, Crossref, or a model connection.
4. Open `author-review.docx`. Word’s Review pane contains comments where the text could be safely located. The visible appendix and `author-queries.md` also list the queries that could not be anchored.
5. Use each finding ID to look up the observation, its location and candidate records in `citation-inventory.json` and `evidence-ledger.json`. The evidence ledger includes a location-to-text index of the snapshot. Use the unchanged `source-snapshot` file to check layout and surrounding text.
6. Copy `decision-ledger.json` to a separate working file. For each finding, record the human decision, the reviewer, date and rationale. If deciding an edition or whether a source supports a statement, examine the edition or passage actually consulted and record that evidence. The issued packet and receipt remain an audit copy.

## Command line

Run these commands in PowerShell from the application folder, using the Python environment where requirements are installed. Substitute private local paths and a real edition label. The inspect command returns one SHA-256.

```powershell
$sourceFile = 'C:\PrivatePress\Inputs\chapter.docx'
$outputFolder = 'C:\PrivatePress\ReviewPackets'
$selectedHash = python -m upress_workbench.offline inspect --source $sourceFile
if ($LASTEXITCODE -ne 0) { throw 'Selection failed' }
$selectedHash
# Compare to the intended draft before issuing the next command.
python -m upress_workbench.offline review --source $sourceFile --sha256 $selectedHash --output $outputFolder --edition 'Editor-selected draft 3' --owner 'Volume editor'
```

The generating account, system and administrators can read a private Windows packet. On POSIX the workbench requests a user-only staging directory. Git, linked paths, known OneDrive roots and Windows network paths are rejected in the private workflow. The operator must choose storage they control; local sync agents, shares and backup destinations cannot all be inferred from a path.

## Read coverage and provenance

The offline parser inventories common author–date citation forms and text following a recognized reference heading. Check its coverage notes on every run. Explicit sample-size notation such as `(n = 2020)` is excluded; ambiguous prose resembling author–date syntax, such as `(draft, 2020)`, may still be inventoried and requires human judgment. Numeric and note-based systems, unusual names, abbreviations, fields, headers, tracked revisions and embedded material may require manual inventory. Check other chapters before treating a local figure or table absence as a volume-wide defect. The single-slice limits include 20 MiB per source, 350,000 extracted characters, 4,000 extracted blocks, 8,000 physical lines in text inputs and 500 author queries.

Author/year, edition statements and DOI strings come from the manuscript. A candidate link can help locate a reference. It cannot establish source identity, accurate quotation, permission or claim support. The receipt marks both model stages as unrun. Starting the desktop workbench normally disables provider-assisted runs and resumes. A separately authorized operator can start a new session with `-EnableProvider` and then use the provider section with its classification and manifest approval. A private run grants no provider approval.

## Failure and retry

A changed hash, refused file type, failed privacy check or unsupported Word package blocks the packet. Keep the source intact. For unsuitable Word content, prepare an explicitly identified flattened copy and inspect its new hash, or inspect the original manually.

A successful run has a `PrivateReview-…` folder containing `run-receipt.json`. The receipt records both original hashes, the implementation fingerprint and SHA-256 for each issued artifact. Check the seven issued files with `python -m upress_workbench.offline verify --packet "FULL_PACKET_FOLDER" --source $sourceFile`. This compares file hashes with the receipt and the snapshot with the selected source. Retain the original receipt separately if you need an independent record of issuance. A retry creates a fresh packet.

Ordinary errors remove the private staging folder. A power loss or forced process exit can leave an ACL-protected `.incomplete-…` folder. List recognized inactive stages with `python -m upress_workbench.offline recover --output $outputFolder`. Malformed stage markers are preserved for manual inspection and do not prevent other recognized inactive stages from being listed. After confirming the prior process has stopped, discard one listed stage with `--discard EXACT_STAGE_NAME` on the same command. Active stages and unexpected files are preserved. A partial or unrecognized folder is never an issued review packet.
