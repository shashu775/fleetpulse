# 1. Pod stuck at `Init:0/1` forever

## What I saw

```
NAME                READY   STATUS       RESTARTS   AGE
consignment-...     0/1     Init:0/1     0          7m
dispatch-...        0/1     Init:0/1     0          7m
web-...             0/1     Init:0/1     0          7m
redis-...           1/1     Running      0          7m
```

Three pods stuck. No error, no crash — just waiting, for seven minutes.

## What `Init:0/1` actually means

Some pods have a **init container**: a little throwaway container that runs *first* and must finish
successfully before the real app is allowed to start. Ours is a tiny BusyBox that says
*"don't start the API until the database and Redis are reachable."*

`Init:0/1` means: **0 of 1 warm-up jobs have finished.** The real container hasn't been started yet
— which is why `kubectl logs` gave me nothing useful.

Think of it as a bouncer at the door. The app isn't refusing to work; it hasn't been let in.

## The actual cause

The init container's log said it plainly:

```
nc: bad address 'redis-service'
```

It was waiting for a machine called `redis-service`. But my Redis Service was named `redis`:

```
NAME     TYPE        CLUSTER-IP
redis    ClusterIP   10.99.86.119     ← the real name
```

So it waited for a name that didn't exist. Forever. The two files simply disagreed:

| File | Said |
|---|---|
| `04-redis.yml` | the Service is called `redis` |
| `05-consignment.yml` | wait for `redis-service` |

## How to read init container logs

This is where I wasted time. You cannot just run `kubectl logs <pod>` — you have to name the init
container, and **its name is not "initcontainer"**:

```powershell
kubectl logs <pod> -c init-consignment -n fleetpulse    # ✅
kubectl logs <pod> -c initcontainer    -n fleetpulse    # ❌ "container is not valid for pod"
```

Get the real names instead of guessing:

```powershell
kubectl get pod <pod> -n fleetpulse -o jsonpath='{.spec.initContainers[*].name}'
```

## The fix

Make both files agree. I renamed the Service to `redis-service`.

⚠️ **But renaming a Service has a knock-on effect.** Anything else pointing at the old name breaks
too — in our case `REDIS_URL` in `01-configmap.yml`. Rename a Service, then immediately grep for the
old name everywhere:

```powershell
Select-String -Path infra\k8s\base\*.yml -Pattern "redis"
```

## A second, sneakier version of this

Later the same error appeared for a name that **did** exist:

```
nc: bad address 'postgres-service'
```

...while `kubectl get svc` clearly listed `postgres-service`. Both were true.

`postgres-service` is a **headless** Service (`CLUSTER-IP: None`). A headless Service has no address
of its own — the name only resolves to *pods that are currently ready*. Postgres wasn't up yet, so
there were no ready pods, so the name resolved to nothing.

A normal Service always resolves, even with zero pods behind it. A headless one doesn't.

So this wasn't a second bug — it was a **symptom of Postgres being broken**, and it fixed itself the
moment Postgres started.

## What I changed permanently

The `web` pod had an init container too — waiting on Postgres and Redis. But `web` is just nginx
serving static files; it has no database connection at all. It was being held hostage by a typo in
someone else's dependency, which meant **a backend problem took the entire UI down**.

I removed it. The website should be the one thing you can still open when the backends are broken.

---

**How to remember it:** `Init:0/1` = *waiting at the door*. Something it's waiting for is spelled
wrong, or genuinely isn't up yet.

**Check it in 5 seconds:**
```powershell
kubectl logs <pod> -c init-<name> -n fleetpulse --tail=3
```
