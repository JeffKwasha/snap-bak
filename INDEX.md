# INDEX

Requirement taxonomy. This file is an index, not a second copy: the requirement
text lives in the document named on its row. The document carries the same ID at
the statement.

To read a requirement, take the ID from the test or function, find its row here,
then grep the ID in the named file (or let QMD find the snippet).

Conventions:

- IDs are `R-<AREA>-<NN>` and stable. Never renumber; retire an ID rather than
  reuse it.
- The document marks each requirement with the ID at the statement:
  `[[R-LOCK-02]] Both transitions log...`.
- A test or function cites every ID it covers on its definition line:
  `def f(...):  # [[R-AREA-NN]] [[R-OTHER-NN]]`. A multi-line signature puts them
  on the `def name(` line, so name, IDs and line number share one grep line.
- The mapping is many-to-many: a requirement may have several tests, and one
  test may cite several requirements.
- Tests point at the docs. This file does not track which tests exist, and a
  missing test is not detected here.

| ID | Document | Section |
|---|---|---|
| [[R-DEPLOY-01]] | SPEC.md | Deployment Contract |
| [[R-DEPLOY-02]] | SPEC.md | Deployment Contract |
| [[R-DEPLOY-03]] | SPEC.md | Deployment Contract |
| [[R-DEPLOY-04]] | SPEC.md | Deployment Contract |
| [[R-DEPLOY-05]] | SPEC.md | Deployment Contract |
| [[R-DEPLOY-06]] | SPEC.md | Deployment Contract |
| [[R-DEPLOY-07]] | SPEC.md | Deployment Contract |
| [[R-LOCK-01]] | SPEC.md | Global Lock |
| [[R-LOCK-02]] | SPEC.md | Global Lock |
| [[R-LOCK-03]] | SPEC.md | Global Lock |
| [[R-LOCK-04]] | SPEC.md | Global Lock |
| [[R-LOCK-05]] | SPEC.md | Global Lock |
| [[R-LOCK-06]] | SPEC.md | Global Lock |
| [[R-STATE-01]] | SPEC.md | State & Runtime Directories |
| [[R-STATE-02]] | SPEC.md | State & Runtime Directories |
| [[R-STATE-03]] | SPEC.md | State & Runtime Directories |
| [[R-STATE-04]] | SPEC.md | State & Runtime Directories |
| [[R-STATE-05]] | SPEC.md | State & Runtime Directories |
| [[R-STATE-06]] | SPEC.md | State & Runtime Directories |
| [[R-CONFIG-01]] | SPEC.md | Configuration |
| [[R-CONFIG-02]] | SPEC.md | Configuration |
| [[R-CONFIG-03]] | SPEC.md | Configuration |
| [[R-CONFIG-04]] | SPEC.md | Configuration |
| [[R-CONFIG-05]] | SPEC.md | Configuration |
| [[R-CONFIG-06]] | SPEC.md | Configuration |
| [[R-REPO-01]] | SPEC.md | Configuration, Repo Schema |
| [[R-REPO-02]] | SPEC.md | Configuration, Repo Schema |
| [[R-REPO-03]] | SPEC.md | Configuration, Repo Schema |
| [[R-REPO-04]] | SPEC.md | Configuration, Repo Schema |
| [[R-REPO-05]] | SPEC.md | Configuration, Repo Schema |
| [[R-REPO-06]] | SPEC.md | Configuration, Repo Schema |
| [[R-REPO-07]] | SPEC.md | Configuration, Repo Schema |
| [[R-JOB-01]] | SPEC.md | Configuration, Job Schema |
| [[R-JOB-02]] | SPEC.md | Configuration, Job Schema |
| [[R-JOB-03]] | SPEC.md | Configuration, Job Schema |
| [[R-JOB-04]] | SPEC.md | Configuration, Job Schema |
| [[R-JOB-05]] | SPEC.md | Configuration, Job Schema |
| [[R-JOB-06]] | SPEC.md | Configuration, Job Schema |
| [[R-JOB-07]] | SPEC.md | Configuration, Job Schema |
| [[R-JOB-08]] | SPEC.md | Configuration, Job Schema |
| [[R-JOB-09]] | SPEC.md | Configuration, Job Schema |
| [[R-JOB-10]] | SPEC.md | Configuration, Job Schema |
| [[R-JOB-11]] | SPEC.md | Configuration, Job Schema |
| [[R-JOB-12]] | SPEC.md | Configuration, Job Schema |
| [[R-JOB-13]] | SPEC.md | Configuration, Job Schema |
| [[R-JOB-14]] | SPEC.md | Configuration, Job Schema |
| [[R-JOB-15]] | SPEC.md | Configuration, Job Schema |
| [[R-JOB-16]] | SPEC.md | Configuration, Job Schema |
| [[R-JOB-17]] | SPEC.md | Configuration, Job Schema |
| [[R-JOB-18]] | SPEC.md | Configuration, Job Schema |
| [[R-JOB-19]] | SPEC.md | Configuration, Job Schema |
| [[R-JOB-20]] | SPEC.md | Configuration, Job Schema |
| [[R-JOB-21]] | SPEC.md | Configuration, Job Schema |
