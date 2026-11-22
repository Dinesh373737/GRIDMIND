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

### Tests

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

MySQL is the relational store. Local mode writes step events to a persistent document journal via a SQL outbox. Optional AWS mode publishes them to the existing DynamoDB table and exports completed runs to private S3; see [AWS preflight, deployment, and verification](docs/AWS.md). The separate **Agent Demo** page is scripted and never creates a simulation or writes to a store.
