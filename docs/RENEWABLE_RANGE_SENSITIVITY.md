# Renewable outlook sensitivity prototype

This offline prototype adds **modeled what-if ranges**, not prediction intervals.
It is not wired into GRIDMIND's default replay or dispatch. To regenerate the
standalone report without downloading any data:

```sh
python scripts/prepare_outlook_sensitivity.py
python -m pytest -q tests/test_outlook_sensitivity.py
```

The command reads the existing `data/austin_public.json`, leaves it unchanged,
and writes `.runtime/analysis/austin_renewable_sensitivity.json`. The report
records the source SHA-256, archive, gridpoint, exact forecast issue and target
times, assumptions, and separate solar and wind factors. Equivalent replay slots
are grouped by forecast issue and target, rather than counted as independent
forecasts. Each forecast must have been published by the replay slot under the
pack's 60-minute publication delay. Later forecasts are not used to construct
the bounds.

## What the range means

- **Solar:** Apply a chosen ±20% relative change to the archived normalized PV
  yield and limit it to 0–100% of nameplate capacity. A zero solar point remains
  zero under this multiplicative scenario. This is not a claim that every zero
  solar forecast is certain.
- **Wind:** Infer equivalent hub speed from an archived wind output factor
  strictly between zero and one under GRIDMIND's virtual cubic turbine curve.
  Apply ±1 m/s to that speed and take the minimum and maximum output from
  GRIDMIND's curve over the speed window, including transitions at rated
  speed and cut-out. A factor of exactly zero or one does not identify a
  unique hub speed, so no range is reported without a separate speed input.
- These default perturbations are fixed what-if assumptions, not fitted error
  margins. Missing points remain missing. They can be overridden explicitly
  with `--pv-yield-delta` and `--wind-speed-delta`. Unitless bounds scale by
  the capacity in kW for the corresponding virtual asset. If adding solar and
  wind bounds, treat the result as allowing both perturbations simultaneously,
  not as a joint probability statement.

All 30 hourly outlooks in the bundled Austin replay have a modeled range for
both sources. For the forecast issued August 24, 2018 at 12:00 UTC, valid
21:00 UTC, the default assumptions yield:

| Seeded participant | Point estimate | Scenario range |
| --- | ---: | ---: |
| Solar and storage cooperative | 31.22 kW | 24.98–37.47 kW |
| Riverside homes | 4.07 kW | 3.26–4.89 kW |
| Community wind cooperative | 0.69 kW | 0.17–1.49 kW |

## Evidence limits

Of the 30 forecasts, 27 match a later replay row for the same target with the
**same** NOAA forecast issue and identical point estimates. One target is not
present in the target-time rows. Only two compare different NOAA cycles for
the same target; both are modeled forecasts, not generation observations.
The wind revisions were +0.10339 and −0.00457 of nameplate output, and the
default scenario did not encompass the first revision. Neither that
comparison nor the physical unit tests establishes prediction coverage.

The NOAA HRRR gridpoint provides forecast inputs for this prototype. The
pack's EIA-930 regional demand and ORNL EAGLE-I outage indicators are not
renewable generation verification. A separate, optional Pecan Street
meter overlay contains measured household solar output for PV evaluation;
it does not provide turbine measurements. A calibrated wind production
interval would need representative, time-aligned forecasts and measured
wind output or hub-height wind, with enough history for chronological
calibration and validation across different operating conditions.

For a separate read-only audit that compares exact same-target NOAA cycles
and optionally scores the old measured PV bands without retuning, run:

```sh
python -m scripts.audit_renewable_ranges
python -m scripts.audit_renewable_ranges --meter .runtime/datasets/austin_meter_overlay.json
```

The second command needs the private optional overlay. Keep that input
and its raw meter data outside Git. The repo's previously documented
PV holdout covered 9 of 29 target readings; that result describes the old
meter-calibrated scenario band, not this physical sensitivity envelope.

The audit uses pandas to join records by UTC target hour and NumPy to
summarize interval widths. Install those two tools in an analysis environment
if needed; they are not dependencies of the deployed application. The
optional command uses complete target hours, keeps the old fit frozen, and
scores the measured aggregate kW rather than clipping it at the normalization
reference. It reports possible decimal-rounding ambiguity separately.

A read-only check of the local overlay's **stored normalized measurements**
found 8/29 hours covered (27.6%); among the 16 hours above the fixed public PV
factor threshold of 0.09, it found 8/16. It compared the first issued forecast
for each target to the mean of the four measured quarter-hour samples. This
is a different scoring protocol from the original documented 9/29 result;
the measurements were not moved off the local computer. The unclipped
aggregate-kW version of the check was not run on the real overlay.

To justify a calibrated interval, collect additional aligned forecast and
measured output history. Split entire days chronologically into fit,
calibration, and untouched test periods, keeping repeated forecast slots
together. Report coverage and mean width overall and by daylight, weather
regime, and lead time. An acceptable calibration result must hold on the
untouched period, not just on the data used to choose the band.
