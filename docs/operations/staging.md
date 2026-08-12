# Staging verification

The `staging` branch remains the deployment source for the hosted staging
environment. Railway's built-in deployment healthcheck must be disabled for the
staging service because that probe runs on the internal service port over HTTP.
This is a hosted-service setting rather than Foundry application code. The
repository instead verifies the public HTTPS surface after the branch is
updated.

## Public HTTPS gate

Set the repository variable `STAGING_URL` to the staging service's public base
URL, without a path, for example:

```text
https://staging.example.com
```

`Verify Staging HTTPS` runs on every push to `staging`, waits for the deployment
to become reachable, and requires `GET /healthz` to return HTTP 200 with
`{"status":"ok"}`. It retries for up to five minutes. The workflow can also
be started manually with a URL input when validating a different deployment.

Other hosting platforms can use the same public HTTPS check with their native
deployment or uptime tooling; no Railway-specific middleware is required in
Foundry.
