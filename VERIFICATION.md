# University Press Workbench verification record

This is the September upstream record for the provider-assisted application. The separate private candidate has its own synthetic pilot and acceptance memo delivered outside the source tree. For this candidate's offline entry points, privacy boundary and verification command, see [Supervised private review](PRIVATE-REVIEW.md).

Verification date: September 10, 2026

Status: **Ready with conditions for supervised local production use.** The application, both pinned skills, a live LunaRoute run, the browser interface, and one generated Word packet were tested. The review found and corrected defects; this record does not claim the system is secure or ready for unattended publication work.

## What passed

- GLM 5.3 foreground produced the file-level architecture and an adversarial design review through LunaRoute. The security pass returned `ready_with_conditions` and identified hostile DOCX content, manuscript prompt injection, credential leakage, source drift, SSRF, schema-valid false findings, and stale evidence as the main threats.
- A live `glm-5.3-background` run completed RCI and SEI in exactly two model calls. Both formal run results validate against their pinned package schemas.
- The synthetic chapter's source SHA-256 remained unchanged. Its embedded instruction to ignore the workbench rules was preserved as manuscript data, not followed, and surfaced to Editorial QA.
- The author copy contained 14 native Word comments, a visible review appendix, no external DOCX relationships, and no accepted edits. Its final three-page layout was inspected page by page in Microsoft Word rendering after the bundled LibreOffice renderer reported that LibreOffice was absent on this host.
- Browser smoke tests returned HTTP 200, served a restrictive CSP, injected a random per-process request token, loaded both pinned skills, scanned one chapter, and rejected a forged cross-origin state change with HTTP 403. No browser warnings or errors were logged.
- The repository secret scan found no embedded LunaRoute, OpenAI, GitHub, Google, or private-key values. The security pattern scan returned no candidates; that result is triage evidence, not proof of security.
- All nine application tests passed on Windows after the release gate corrected package-checkout line endings, short-path source containment, Windows process liveness checks, and SQLite connection closure.

## Automated checks

The repository CI repeats these checks on Windows:

```powershell
python -m unittest discover -s tests -v
python -m compileall -q upress_workbench
node --check upress_workbench/static/app.js
python vendor/reference-citation-integrity/scripts/validate_package.py
python vendor/reference-citation-integrity/evals/scorer.py --validate-suite
python vendor/scholarly-editorial-integrity/scripts/validate_package.py
python vendor/scholarly-editorial-integrity/evals/scorer.py --validate-suite --self-test
python scripts/check_no_secrets.py
ruff check .
bandit -q -r upress_workbench -s B606
pip-audit -r requirements.txt
```

The RCI suite contains 51 fixtures across 23 rules. The SEI suite contains 42 fixtures across 30 rules; its supplied self-test scored 42 of 42, including 20 of 20 zero-tolerance checks.

## Conditions before real chapter work

1. Rotate the LunaRoute credential that may have appeared in prior Codex history. Never commit the replacement.
2. Have a University Press steward approve `config/company-constitution.json`, `config/workbench-configuration.json`, the APA profile, and the unresolved decision hooks.
3. Confirm that LunaRoute is approved for each exact manuscript manifest. Restricted manuscripts remain blocked.
4. Run a supervised pilot on several representative chapters and review false positives, missed citations, comments, and author-facing tone.
5. Keep human acceptance and publication outside this application.

The current limits are deliberate: DOCX, Markdown, and UTF-8 text only; no PDF or OCR; no arbitrary URL fetches; no full-text source verification unless evidence is supplied; no automatic acceptance; and no publication action.
