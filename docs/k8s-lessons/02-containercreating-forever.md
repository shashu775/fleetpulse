# 2. Stuck at `ContainerCreating` forever

## What I saw

```
postgres-0     0/1   ContainerCreating   0   7m
db-migrate-... 0/1   ContainerCreating   0   7m
```

Seven minutes of "creating". No crash, no restart count climbing, no logs (`kubectl logs` says the
pod is still initialising). Completely silent.

## Where the reason is hiding

`kubectl get pods` will never tell you why. The reason is at the **bottom** of `describe`, under
`Events:`:

```powershell
kubectl describe pod postgres-0 -n fleetpulse
```

```
Warning  FailedMount  66s (x11 over 7m)  kubelet
  MountVolume.SetUp failed for volume "init" : configmap "postgres-init-scripts" not found
```

There it is. **`Events:` is the single most useful thing in `describe`** — scroll straight to it.

## What it means in plain English

A pod is only allowed to start once **every** folder it expects has been attached. My Postgres pod
expected a folder containing the database setup script (`init.sql`). That folder comes from a
**ConfigMap** — Kubernetes' way of handing a file to a pod.

The ConfigMap didn't exist. So the folder couldn't be attached. So the pod couldn't start. It isn't
an error the pod can recover from by retrying the app — it just waits, retrying the mount, forever.

`x11 over 7m` means it had tried 11 times. It will keep trying indefinitely, which is actually good:
create the missing ConfigMap and the pod starts on its own, no restart needed.

## The fix

```powershell
kubectl create configmap postgres-init-scripts -n fleetpulse --from-file=db/init.sql
```

Within a minute the pod started by itself.

## The trap that got me twice

I hit this **again** later, and the reason is important:

> **This ConfigMap is not in `infra/k8s/base/`, so `kubectl apply -f` does not create it.**

I made it by hand with `kubectl create`. That means every time I ran
`kubectl delete namespace fleetpulse` to start fresh, it vanished with everything else — and
`kubectl apply -f infra/k8s/base/` didn't bring it back, because it was never in there.

**Order matters too.** The ConfigMap must exist *before* Postgres starts:

```powershell
kubectl create configmap postgres-init-scripts -n fleetpulse --from-file=db/init.sql
kubectl apply -f infra/k8s/base/
```

## The other half of the same problem

There were **two different names for one file**:

| File | Wanted |
|---|---|
| `03-postgres.yml` | `postgres-init-scripts` |
| `10-migration-job.yaml` | `db-init-sql` |

So creating one ConfigMap fixed Postgres and left the migration Job stuck — same error, different
name. Fixed by pointing both files at the same one.

This matters beyond "make it start": if you create *both* ConfigMaps, you now have **two copies of
your database schema** in the cluster. Edit one, forget the other, and the StatefulSet and the Job
start setting up different databases. One name, always.

## Worth doing properly later

Creating it by hand is the weak point. A `kustomization.yaml` with a `configMapGenerator` would
build it from `db/init.sql` as part of `apply`, so it can never be forgotten:

```yaml
configMapGenerator:
  - name: postgres-init-scripts
    files: [../../db/init.sql]
```

---

**How to remember it:** `ContainerCreating` = *the pod is waiting for a file or folder that isn't
there.* It is never an app bug — the app hasn't run yet.

**Check it in 5 seconds:**
```powershell
kubectl describe pod <name> -n fleetpulse | Select-String -Pattern "Events:" -Context 0,10
```
