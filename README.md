# GRIDMIND

GRIDMIND replays historical energy and weather in a virtual locality. Register participants, configure their loads and energy assets, and run a replay. When the simulated locality disconnects from its upstream grid, eligible participants can trade the local supply that remains available.

### Run locally on Windows

Use Python 3.11 or newer, MySQL Server 8.0.16 or newer, and Node.js 22.12+ LTS. From PowerShell in the repository:

```powershell
.\scripts\start.ps1
```

Open [GRIDMIND](http://127.0.0.1:8000). The script builds the React dashboard locally with npm and creates a Python environment if needed and uses a project-owned MySQL instance on port **3307** with application files under `.runtime`. Set `GRIDMIND_MYSQL_BIN` if MySQL is installed outside the standard location. To use an existing instance, set `GRIDMIND_DATABASE_URL` to a dedicated MySQL schema URL. The app initializes its tables and reporting view on startup. Run one API process per database, with one additional Python worker per active locality. **Stop** in the dashboard stops the selected simulation. Ctrl+C in the server terminal shuts down the API and its workers.

The application binds to loopback. An optional `GRIDMIND_API_KEY` protects the data API; the dashboard prompts for it when configured. Keep the database URL and credentials in local configuration. No AWS tools, credentials, runtime data feeds, or model services are required. Initial dependency installation uses the normal npm and Python package registries. For React development run `npm ci` and `npm run dev` from `frontend` while the local API is running on port 8000.

### Demo

1. Open **Participants & Localities** and create or edit a participant in the seeded Austin locality. Give a producer a battery power and energy rating to provide an island-forming source for this simulation.
2. From **Live P2P Market**, start the historical Austin replay. The feeder walkthrough above the real market is illustrative; it can be replayed without writing anything. The default window opens the emergency market at step 8. The grid reconnects at step 13, using zero-based step numbers. The dashboard shows seller supply, buy requests, and trade results.
3. While the run is active, stage a buy or sell on a future step. Limits are in scenario credits per kWh. A 36 kW order for one 15-minute interval requests 9 kWh. The submitted order replaces that participant's automatic order on the same side in that interval.
4. Open **Agent Activity** for the saved decisions and **Renewable Outlook** for advice based on the archived forecast. The preview computes the next step without settling it.
5. Start a **Reference** run under the same scenario, settings, roster, and manual-order inputs for a like-for-like comparison. **Database & Reports** reads aggregate results from MySQL. To see process isolation, create a second locality, give it participants, and start its run while the first is active.

### Data and model scope

The bundled replay uses NOAA weather forecasts, ORNL county outage records, and EIA regional demand. The public pack uses a regional demand shape for configured participant loads. See [data preparation and licences](docs/DATA.md) for the optional local Austin meter overlay and µGIM trading benchmark. Data packages are versioned by content in SQL, and a run pins its imported version and seed. Stress scenarios sample small demand variations from archived demand residuals. The optional **Weather variability** setting adds reproducible cloudy, rainy, and windy periods derived from the replay. Source observations and archived forecasts keep their original values and provenance.

The displayed next-hour risk estimates the modeled locality breaching its import limit using historical demand variation and the configured trip rule. It is a simulator estimate. County outage counts describe reporting across a region. They cannot identify which sampled homes lost power or train a feeder-level fault probability. The selected historical event starts and ends the virtual outage by scenario design; it does not establish a causal link to those homes. Forecasts use archived weather to estimate generation and advise users. The checked-in weather pack and short local meter window do not establish a calibrated regional outage classifier or a universally accurate renewable forecast.

The virtual microgrid assumes eligible solar and wind can contribute during islanding when a configured battery can form the island. Capacity, battery limits, urgency, priority policy and bid prices are scenario inputs. See [database design and queries](docs/DBMS.md).

### Austin renewable ranges and model error

Start a **new** Austin replay and open **Renewable Outlook**. The standard public replay shows two separate quantities: **Scenario range** (what the virtual asset would produce under stated changes to the archived forecast), and **Model error margin** (how far the point estimate may be from a later weather-model reference). Neither is a prediction interval for measured electricity generation. Saved runs keep their original dataset and results. Meter-overlay intervals and weather-varied intervals do not inherit margins learned for the unmodified public forecasts.

**Clock and preprocessing.** Group the quarter-hour replay rows by the NOAA forecast's UTC issue and target timestamps. Keep the first available occurrence and require `issue + 60 minutes ≤ replay time < target`. The 120 rows represent 30 unique forecasts; repeated quarter-hour rows aren't independent examples. Keep source data unchanged and check its SHA-256 before joining the derived results.

**Scenario formulas.** Factors are fractions of nameplate power, so an asset rated `C` kW has estimated output `C × factor`.

- **Solar:** With irradiance `G` (W/m²) and ambient temperature `T` (°C), the virtual panel's temperature is `T_cell = T + 0.028G` and its output factor is `s = clip((G/1000) × [1 − 0.004(T_cell − 25)], 0, 1)`. The scenario is `[max(0, 0.8s), min(1, 1.2s)]`.
- **Wind:** Hub speed is `v = sqrt(U² + V²)` from the 80 m wind components. The virtual turbine factor is zero below 3 m/s and at or above 25 m/s; otherwise `w(v) = min(1, (v³ − 3³)/(12³ − 3³))`. For a bundled point factor strictly between zero and one, invert with `v = [3³ + w × (12³ − 3³)]^(1/3)`. The wind scenario takes the minimum and maximum of `w` on `[max(0, v − 1), v + 1]`, including rated speed and cut-out if crossed.
- **Combined participant:** Add capacity-scaled lower endpoints and upper endpoints across installed renewable sources. These are simultaneous what-if bounds, not a joint probability distribution.

**Learned error margin.** Fit a separate ridge regression for solar and wind. The archived forecast factor `p` stays the point estimate; the training label is `e = |p_analysis − p|`, with the later NOAA HRRR analysis at the same Austin grid point and target hour passed through the same power curves. The analysis is a modeled weather reference, not an electricity meter. For a forecast lead of `h` hours, use `x = [1, p, p², h/13]`. Fit coefficients by minimizing `Σ(e − xβ)² + 0.10 × Σβⱼ²` over the non-intercept coefficients. The initial half-width is `g(x) = clip(xβ, 0, 1)`. On the separate calibration days, sort `e − g(x)` and take its `ceil(0.80 × (n_cal + 1))`-th smallest value `q`. The reported factor margin is `m = clip(g(x) + max(0, q), 0, 1)`; the `±` kW margin is `Σ Cᵢmᵢ` across installed sources. For single-source validation, compare the later analysis to `[max(0, p − m), min(1, p + m)]`. The margin is displayed separately; it does not silently widen the what-if bounds or affect trading.

The model panel uses August 2018 Austin HRRR forecasts issued at 12:00 UTC, at leads 2, 8, and 13 hours, paired to same-target HRRR analyses. Dates are split by forecast issue day: August 9–15 for fitting, August 16–19 for selecting the margin adjustment, and August 20–23 for a final held-out check. All precede the bundled replay forecast cycles. The historical sample is short and drawn from one issue cycle, so its proxy coverage and nominal calibration setting are not guarantees for other cycles, weather regimes, or actual generation. Pecan Street meter data was audited separately and is not used to train this artifact; EIA demand, ORNL outage data, and the Porto P2P benchmark do not train the margins.

| Held-out metric (against the HRRR modeled reference) | Solar | Wind |
| --- | ---: | ---: |
| Fit / calibration / test pairs | 19 / 11 / 8 | 21 / 12 / 9 |
| Test targets within the margin | 7/8 | 8/9 |
| Point MAE (% of nameplate) | 1.93% | 2.60% |
| Mean reported half-width (% of nameplate) | 6.26% | 5.45% |
| Mean full width after physical clipping (% of nameplate) | 12.32% | 6.93% |

A fixed-margin baseline (start with mean fit-set absolute error, then use the
same residual calibration rule) covered 7/8 solar and 9/9 wind targets, with
mean clipped widths of 12.66% and 10.06% of nameplate. The adaptive model
was narrower on this sample, with the same solar coverage and one fewer
wind hit. This small comparison does not establish that ridge performs better
in general.

The final evaluations span three distinct issue days for each source. The preparation requested 45 issue–target pairs and retained available fields without filling archive gaps (11 products were unavailable). It uses DSWRF surface irradiance from the original NOAA GRIB2 and 2 m temperature plus 80 m U/V wind from the University of Utah HRRR Zarr archive, at the Austin grid point (indexed x=891, y=223). The [checked-in training data](data/austin_renewable_ml_training.json) stores only derived public grid-point inputs and source checksums. The [derived replay artifact](data/austin_renewable_ml.json) stores trained coefficients, held-out metrics, and the 30 scenario ranges and margins. To reproduce the fit and artifact offline:

```sh
python -m scripts.train_renewable_ml
```

The app reads the committed data at startup; no packages or weather downloads are required at runtime beyond the normal app dependencies. SHA-256 checks use LF line endings; Git keeps the data JSON files in that format, and existing CRLF checkouts are normalized before hashing. Re-fetching the archived public inputs is optional: install `eccodes`, `numcodecs`, and `numpy` into an analysis environment and run `python -m scripts.fetch_renewable_ml_training` before training. See [the exploratory range report](docs/RENEWABLE_RANGE_SENSITIVITY.md) for the original offline scenario prototype and separate local meter check.

### Tests

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

MySQL is the relational store. Local mode writes step events to a persistent document journal via a SQL outbox. Optional AWS mode publishes them to the existing DynamoDB table and exports completed runs to private S3; see [AWS preflight, deployment, and verification](docs/AWS.md). The separate **Agent Demo** page is scripted and never creates a simulation or writes to a store.

**Explain this run** in Run History summarizes a completed simulation. If the backend is explicitly configured for Groq, GRIDMIND sends only aggregate run statistics and caches a successful explanation. Without a key, or if the model request fails, GRIDMIND explains the run directly from its saved statistics. Normal reports and local simulations require neither Groq nor AWS. See the [deployment runbook](docs/AWS.md#on-demand-run-explanations).

The [backend environment template](.example.env) lists the optional Groq settings. The app reads them from the backend process environment; it does not load this template automatically. Put a real key only in private backend configuration, never in the template or frontend.
