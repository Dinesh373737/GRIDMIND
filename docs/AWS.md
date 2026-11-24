# GRIDMIND on the existing AWS resources

This is a deployment/runbook, not evidence that AWS was changed. **No AWS commands that change state until billing and inventory preflight pass. Keep the Free account plan.** The normal Windows MySQL startup still requires no AWS software, credentials, or network calls.

## 1. Read-only billing and inventory gate

1. Identify the account (and region(s)) containing the existing Amplify app, Cognito pool, `gridmind-backend`, `gridmind-events` and `gridmind-exports-el`. Compare account IDs. Use a read-only billing identity for the first pass. Do not switch plans, sign up for AWS Organizations, activate another paid service, or accept prompts to upgrade.
2. In [Billing and Cost Management](https://console.aws.amazon.com/billing/), verify **Free account plan**, expiry date/days remaining, actual credit balance and expiry, and credit applicability to the specific services usage. Review Free Tier and this month's Bills by service for gross usage/current costs as well as the credit offset and net amount. Record the timestamp and current running resources. A reported $120 is not a verified balance. [AWS plan details](https://docs.aws.amazon.com/awsaccountbilling/latest/aboutv2/free-tier-plans.html).
3. Read the instance ID, state, instance type, CPU credit mode, AMI/OS, EBS volume sizes/types, snapshots, public IPv4/EIP, attachment state, current endpoint/TLS DNS, existing instance profile and authorized management access. Then review Amplify auto-build branches and artifacts, Cognito app client details (public browser client; authorization-code flow with PKCE; only approved demo users), DynamoDB keys/billing mode, and bucket region, encryption, versioning, and access settings. Verify current usage, prices and credit coverage before any build, test, storage write or start. Stop preflight on any unknown or plausible out-of-pocket charge. Do not trigger a deployment build, including an auto-build from an authorized `main` update, until preflight passes.
4. Reuse the known EC2 instance and existing internet-facing HTTPS endpoint; do not stand up a paid load balancer, NAT gateway, hosted zone, extra IPv4, snapshot, RDS or new database/table/bucket as a workaround. If no existing endpoint can safely provide HTTPS to Amplify, pause rather than expose raw HTTP or port 8000. A temporary direct-IP HTTPS demo is an exception only with explicit approval for the exact ingress and deployment window (see below). Changing branch build settings may trigger a billable build; inspect first.

## 2. Early before-credit budget (notification only)

In [AWS Budgets](https://console.aws.amazon.com/billing/home#/budgets), verify that a monitoring-only cost budget is supported on the existing plan and that the selected controls/pricing do not add fees. Choose **Customize (advanced) → Cost budget → Monthly**, an account-wide **$5 USD** limit as a conservative early threshold, choose **Unblended cost**, and disable **Include credits** (do not use a net-after-credits or zero-spend template). Leave other cost-type inclusions visible and record exactly which were selected. Enable an **Actual 50%** email alert ($2.50) and **Actual 100%** email alert ($5) to the owner’s authorized address; confirm there is no identical budget before creating this one. Add no budget actions, SNS, or extra service unless required and separately approved. Verify the budget settings and actual recipient after creation. Alerts lag; this is not a hard cap. [AWS Budgets cost options](https://docs.aws.amazon.com/awsaccountbilling/latest/aboutv2/budgets-create-filters.html).

## 3. Install the tested branch on the existing host

- Use only the existing supported remote access (for example, already-working Systems Manager or SSH). Do not open a new inbound management port to the internet. Use the confirmed AMI package manager to install Python and MySQL on the same host if needed, only after the safety gate. Bind MySQL to loopback, create a dedicated GRIDMIND app database/user limited to its schema, and use a URL-encoded password in `/etc/gridmind/gridmind.env` (root-owned, mode 0600). Do not log or commit the database URL, AWS keys, demo credentials or tokens.
- On the existing branch, install `python -m pip install -r requirements-cloud.txt` into a Python 3.11+ virtual environment at `/opt/gridmind/.venv`. Copy the tested branch or immutable artifact to `/opt/gridmind`. Configure `deploy/gridmind.env.example` with the actual values, and install the provided systemd unit. Use one API worker because it also supervises locality worker processes. The generated state directory contains local event mirrors and worker logs on the instance's existing disk.
- AWS access comes from the existing instance profile, not a static secret. Verify it is associated before running the API. Least privilege: `dynamodb:DescribeTable`, `dynamodb:PutItem` and `dynamodb:GetItem` on the named table; `s3:PutObject` and `s3:GetObject` only below the approved prefix in the named bucket. If verified bucket settings use KMS, verify coverage and minimum required KMS permissions separately. Do not expose the S3 bucket or change IAM/network security without required authorization. The table must have a string run-ID partition key and a separate numeric or string step sort key. Match configured names/types to the *actual* existing table; if incompatible, pause without altering the resource.
- Configure the existing reverse proxy to send HTTPS requests to loopback port 8000. Keep the API endpoint explicit in Amplify config, restrict CORS to the exact Amplify origin, use Cognito access tokens, and ensure the existing user pool/app-client has no unintended self-sign-up or unapproved demo participants. Preserve the existing auth and production configuration unless a planned change has cleared the gate. If the existing instance's public IP changes on restart, recheck the existing DNS/TLS route; do not allocate a new static IP automatically.
- Confirm the budget and preflight before starting a stopped instance. Set a short deployment/demo window, and arrange to stop it before starting. Never change its plan or instance type to make deployment work.

## 4. Verify the same run ID everywhere, then stop the instance

From the deployed UI sign in with the existing Cognito demo user. Use one test locality and one short, non-reference run. Save the UUID returned by the API. Verify `/api/health` reports MySQL and DynamoDB, the run reaches `completed`, and the market timeline reflects the same UUID. On the instance with the **same nonlogged configuration** and instance profile, first validate the table keys using `python -m scripts.verify_cloud_run --check-schema`, then verify the completed run using `python -m scripts.verify_cloud_run RUN_UUID`. This independently reads MySQL, strongly consistent DynamoDB events and the private S3 JSON, checks the per-step IDs, order, timestamps and checksums, and checks that DynamoDB event content matches the S3 export. Capture the UUID, branch commit, UTC timestamp, counts, and export key. Do not claim live verification from local tests.

At the end of deployment/demo, confirm no live audience or active simulation still needs the instance. Use EC2 **Stop**, never Terminate, and confirm state **stopped**. Persisted EBS volumes, snapshots and some public addresses can still draw credits while stopped. Recheck billing after AWS posts the usage. If interrupted after starting, stopping and confirming the state takes priority. [EC2 stop/start](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/Stop_Start.html).

### Amplify React build values

Use the repository-root `amplify.yml` (Node 22.12+ or another Vite-supported LTS release). On the approved release branch (use `main` only with the repository owner's authorization and after preflight), set these public build-time values; never place secrets in Vite variables:

- `VITE_API_BASE_URL`: existing HTTPS API origin, without `/api`.
- `VITE_COGNITO_DOMAIN`: existing Cognito HTTPS managed-login origin.
- `VITE_COGNITO_CLIENT_ID`: existing public app client ID, matching the backend verifier.

Allow the exact Amplify URL with trailing slash as both callback and logout URL. Use authorization code + PKCE with the `openid` scope, no app client secret. Verify the Amplify branch's default route opens the React dashboard, and confirm sign-out and the authorization gate before testing a run. Do not change the existing Amplify app or create new hosting until the billing gate passes.


### Approved temporary public-IP HTTPS demo

This only applies when the owner approves temporary demo ingress. Run the
normal billing, budget, client and instance-role gates first. Reserve a short
demo window, check the EC2 instance-initiated shutdown behavior is **stop**
before relying on an in-guest shutdown timer, and independently verify that
EC2 stops when the window ends. Do not allocate an Elastic IP or add a new
cloud service, and do not open SSH, port 8000 or port 3306.

1. Start the existing instance for the demo and use Session Manager for
   administration. Keep FastAPI and MySQL bound to loopback. Capture the
   assigned public IPv4 and confirm that it belongs to this instance.
2. With the approved temporary port 80 rule, issue a
   [short-lived IP certificate](https://letsencrypt.org/2026/03/11/shorter-certs-certbot)
   using an official ACME client supporting IP identifiers. Use HTTP only for
   the certificate challenge, never for the API. Remove the port 80 rule as
   soon as validation finishes, including on failure. Expose port 443 only
   through a TLS reverse proxy to loopback FastAPI. Verify the IP SAN, trust
   chain, HTTPS health and signed-in origin allowlist without bypassing TLS.
3. For this short demo, set `VITE_API_BASE_URL=https://PUBLIC_IPV4` and
   `VITE_GRIDMIND_DEMO_EXPIRES_AT=YYYY-MM-DDTHH:MM:SSZ` on the approved
   Amplify branch, with a UTC cutoff **before** the planned removal of access
   and shutdown. Verify the browser build blocks sign-in and API traffic
   after cutoff, clears local auth state, and doesn't send a bearer token to
   a stale IP. The expiry is a defense for the planned teardown, not a
   permanent hostname; do not reuse an old build or trust its endpoint after
   the instance releases its IP.
4. After the test run, expire demo browser access, remove the demo-only 443
   rule and stop the instance. Verify both removal and the **stopped** state.
   A later demo may require a fresh IP, certificate and frontend build.
   Never use this temporary route as an always-on production URL.

### On-demand run explanations

**Explain this run** works without an external provider. The built-in summary
is assembled from saved run statistics and is labeled separately from AI
output. Normal simulation reports do not depend on either explanation path.

To enable Groq on the existing EC2 backend:

1. Verify the Groq project is on the Free plan and that the selected model
   `openai/gpt-oss-20b` is available. Check the
   [Groq rate limits](https://console.groq.com/docs/rate-limits). AWS credits
   do not apply to Groq. Do not upgrade plans or add a payment method.
2. Keep `GRIDMIND_GROQ_ENABLED=0` until a fresh, private key is available.
   Use an approved private admin session that does not record secret input.
   Enter `GROQ_API_KEY` in the existing root-owned, mode-0600 systemd
   EnvironmentFile on EC2, not in a command argument. Set
   `GRIDMIND_GROQ_ENABLED=1` in that file only when approved.
   Never paste the key into chat, Git, Amplify, Vite variables, cloud command
   payloads, or logs. Revoke any key previously shared that way.
3. The backend uses outbound HTTPS to
   `https://api.groq.com/openai/v1/chat/completions`. No new incoming network
   access or AWS IAM grants are needed. Only compact run aggregates are sent,
   not run IDs, participant details, logs, or credentials. Inference is
   on-demand, with no background activity. A database claim caps Groq at one
   attempt per completed run, including timeouts, and caches a successful
   response. A failed attempt on that run continues to use the saved-data
   fallback rather than retrying.
4. On the deployed backend, request an explanation for one known completed
   run; verify `source: groq` and that the repeated request reads the cached
   explanation. With the provider disabled, verify that a completed run
   returns a directly generated summary. Test timeouts and rate limits through
   a mock rather than exhausting the live service. Check the normal report
   independently, then stop EC2 and confirm it stopped.

No Bedrock calls are used by this feature. Existing Bedrock result records do
not need to be deleted. If Groq is disabled, no key is configured, or the
provider fails, the built-in summary remains available.
