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
`{"status":"ok"}`. It also verifies that plain HTTP `/healthz` redirects to
HTTPS. It retries up to 30 times with a 10-second request timeout and a
10-second delay between attempts—roughly ten minutes in the worst case—before
failing closed. The workflow can also be started manually with a URL input when
validating a different deployment.

Staging must set `DJANGO_TRUST_PROXY_HEADERS=true`. Railway terminates TLS
before forwarding requests to Foundry; this setting enables Django to honor
the forwarded HTTPS marker and enforce the redirect/HSTS policy at the public
edge.

Other hosting platforms can use the same public HTTPS check with their native
deployment or uptime tooling; no Railway-specific middleware is required in
Foundry.
