# Stress calibration reports

One folder per task set, `docs/calibration/<world>-<version>/`, holding the `report.json` and
`report.md` that `sandbox stress calibrate` writes (`stress-calibration-v1`). Only the reports are
committed; the calibration's run directories stay outside git. `sandbox release-check` reads
`docs/calibration/<set>/report.json` by default.

No report is here yet: the calibration needs real models (`docs/STRESS.md`, `docs/RELEASING.md`).
