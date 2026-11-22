# GRIDMIND on the existing AWS resources

This is a deployment/runbook, not evidence that AWS was changed. **No AWS commands that change state until billing and inventory preflight pass. Keep the Free account plan.** The normal Windows MySQL startup still requires no AWS software, credentials, or network calls.

## 1. Read-only billing and inventory gate

1. Identify the account (and region(s)) containing the existing Amplify app, Cognito pool, `gridmind-backend`, `gridmind-events` and `gridmind-exports-el`. Compare account IDs. Use a read-only billing identity for the first pass. Do not switch plans, sign up for AWS Organizations, activate another paid service, or accept prompts to upgrade.
2. In [Billing and Cost Management](https://console.aws.amazon.com/billing/), verify **Free account plan**, expiry date/days remaining, actual credit balance and expiry, and credit applicability to the specific services usage. Review Free Tier and this month's Bills by service for gross usage/current costs as well as the credit offset and net amount. Record the timestamp and current running resources. A reported $120 is not a verified balance. [AWS plan details](https://docs.aws.amazon.com/awsaccountbilling/latest/aboutv2/free-tier-plans.html).
3. Read the instance ID, state, instance type, CPU credit mode, AMI/OS, EBS volume sizes/types, snapshots, public IPv4/EIP, attachment state, current endpoint/TLS DNS, existing instance profile and authorized management access. Then review Amplify auto-build branches and artifacts, Cognito app client details (public browser client; authorization-code flow with PKCE; only approved demo users), DynamoDB keys/billing mode, and bucket region, encryption, versioning, and access settings. Verify current usage, prices and credit coverage before any build, test, storage write or start. Stop preflight on any unknown or plausible out-of-pocket charge. Do not trigger a deployment build, including an auto-build from an authorized `main` update, until preflight passes.
4. Reuse the known EC2 instance and existing internet-facing HTTPS endpoint; do not stand up a paid load balancer, NAT gateway, hosted zone, extra IPv4, snapshot, RDS or new database/table/bucket as a workaround. If no existing endpoint can safely provide HTTPS to the Amplify frontend, pause rather than expose raw HTTP/port 8000. Changing branch build settings may trigger a billable build; inspect first.

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


### On-demand Bedrock run explanations

Regular reports do not depend on Bedrock. The run history can request an optional
explanation of a completed simulation. Keep `GRIDMIND_BEDROCK_ENABLED=0` until
the exact account, region, model access, applicable credit, and current usage
are verified. Enabling it in cloud mode allows only the regional Amazon Nova
Lite model `amazon.nova-lite-v1:0` in `us-east-1`, not a cross-region inference
profile. There is no background invocation. A database claim allows at most
one inference attempt per run, even after a timeout. The SDK does not retry;
the response is limited to 220 tokens and a successful result is stored in
MySQL. Only compact numeric run aggregates
are sent; participant names, account details, raw event logs, and credentials
are not sent. An explanation is supplementary, not a replacement for the
structured report.

For `Converse`, the EC2 instance profile needs the following single-model IAM
statement (review and obtain any required permission before applying it). Do
not grant Bedrock Marketplace actions, broad Bedrock access, or static keys:

```json
{
  "Effect": "Allow",
  "Action": "bedrock:InvokeModel",
  "Resource": "arn:aws:bedrock:us-east-1::foundation-model/amazon.nova-lite-v1:0"
}
```

[Model and regional support](https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-amazon-nova-lite.html)
· [Converse permissions](https://docs.aws.amazon.com/bedrock/latest/userguide/conversation-inference.html)
· [Bedrock pricing](https://aws.amazon.com/bedrock/pricing/).
Until account coverage and authorization are confirmed, use the disabled mode
and do not make a test call. For a gated smoke test, explain one completed run,
refresh the page, and verify the next request uses the persisted result rather
than calling the model again. Check that disabled and unavailable states leave
the ordinary run report visible.
