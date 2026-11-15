# 3. `CrashLoopBackOff` — the app is fine, the address is wrong

## What I saw

```
consignment-...   0/1   Error              4 (83s ago)
dispatch-...      0/1   CrashLoopBackOff   4 (14s ago)
```

Worse: sometimes they showed `1/1 Running`. I'd caught them **between** restarts. The giveaway is
the `RESTARTS` column climbing — a genuinely healthy pod stays at 0.

`CrashLoopBackOff` just means *"it keeps dying, so I'm waiting longer between attempts."* It's not a
cause, it's Kubernetes giving up on rapid retries.

## The log said exactly what was wrong

```
WARNING psycopg.pool error connecting: [Errno -3] Temporary failure in name resolution
psycopg_pool.PoolTimeout: pool initialization incomplete after 30 sec
ERROR:    Application startup failed. Exiting.
```

**"Temporary failure in name resolution" = I was given a computer name, and nobody answers to it.**

It's the network equivalent of dialling a phone number that isn't assigned. Nothing to do with
passwords, permissions, or the database being slow. The address itself was wrong.

## The cause

The connection string had the wrong hostname:

```
postgresql://fleetpulse:fleetpulse@postgresql-service:5432/fleetpulse
                                   ^^^^^^^^^^^^^^^^^
```

The actual Service is **`postgres-service`** — no `ql`.

This one is genuinely easy to misread, because **the front of the URL really is spelled
`postgresql://`.** That's the *protocol*. The bit after the `@` is the *machine name*, and they are
not the same word. I got this wrong twice — first as `postgresql`, then as `postgresql-service`.

## How names become addresses

Every Service you create becomes a working hostname inside the cluster, automatically:

```
Service named "postgres-service"  →  http://postgres-service  works from any pod
Service named "redis-service"     →  redis://redis-service:6379
```

So the list of valid hostnames is literally the output of:

```powershell
kubectl get svc -n fleetpulse
```

If the name in your config isn't in that list, it will not resolve. That's the whole rule.

## Two things that cost me time

**1. `kubectl logs` shows the wrong attempt.** On a crash-looping pod it shows the *current* attempt,
which is usually mid-startup and hasn't failed yet. The crash you care about is the previous one:

```powershell
kubectl logs <pod> -n fleetpulse --previous
```

That's where the real traceback lives.

**2. Editing a Secret does NOT restart the pods.** I fixed the connection string, re-applied, and
nothing changed — because running pods keep the environment variables they were born with. You have
to force new ones:

```powershell
kubectl apply -f infra/k8s/base/02-secrets.yml
kubectl rollout restart deploy/consignment deploy/dispatch -n fleetpulse
```

Forgetting the second line makes it look like your fix didn't work.

## The silent cousin — wrong *key*, not wrong value

My ConfigMap had these:

```yaml
Redis_URL: "redis://redis-service:6379/0"
consignement_URL: "http://consignment-service:8000"
```

The code reads `REDIS_URL` and `CONSIGNMENT_URL`. **Environment variable names are case-sensitive**,
and one was misspelled as well.

So the app never saw them — and instead of crashing, it quietly used its own built-in default
(`redis://redis:6379/0`). That default happened to work for a while, because an old `redis` Service
was still lying around. Delete that leftover and Redis breaks, with nothing in any config file to
explain why.

**A wrong value crashes loudly. A wrong key falls back silently.** The second is far more dangerous.

---

**How to remember it:** "name resolution" failure = *wrong address, not a broken app*. Compare the
hostname against `kubectl get svc`.

**Check it in 5 seconds:**
```powershell
kubectl get secret fleetpulse-secrets -n fleetpulse -o jsonpath='{.data.DATABASE_URL}' | ForEach-Object { [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($_)) }
kubectl get svc -n fleetpulse
```
Do the two names match?
