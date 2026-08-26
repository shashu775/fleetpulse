# Kubernetes lessons — things that broke, in plain English

Written after getting `infra/k8s/base/` running on Docker Desktop for the first time. Every entry
here is something that actually happened, not something from a tutorial.

## The one lesson behind almost all of them

> **Kubernetes connects things by name, and nothing checks that the names match.**

There is no compiler here. You can write `redis-service` in one file and `redis` in another, and
Kubernetes will cheerfully accept both files, create both objects, and report everything as fine.
The failure only shows up later, somewhere else, as a pod that won't start.

Out of roughly nine problems hit in one session, **seven were a name in file A not matching a name
in file B.** Not one was an architecture problem.

So when something breaks, the first question is never "is my design wrong?" It is:

> *What name is this thing looking for, and does anything actually answer to that name?*

## Triage: match the STATUS column

`kubectl get pods -n fleetpulse` tells you which page to read.

| What you see | What it means | Page |
|---|---|---|
| `Init:0/1` | Pod is waiting for something before it even starts | [01](01-pod-stuck-init.md) |
| `ContainerCreating` (stuck) | A file or folder the pod needs doesn't exist yet | [02](02-containercreating-forever.md) |
| `CrashLoopBackOff` / `Error` | The app started, then died. It's a real error — go read the log | [03](03-crashloop-dns.md) |
| Everything Running but data is wrong/missing | Database was set up once and remembers it | [04](04-postgres-init-runs-once.md) |
| Everything Running but the website won't open | Nothing is listening on port 80 | [05](05-ingress-does-nothing.md) |

## The four commands that found every problem

```powershell
kubectl get pods -n fleetpulse              # what state is everything in
kubectl describe pod <name> -n fleetpulse   # scroll to Events: at the bottom — the reason lives there
kubectl logs <pod> -n fleetpulse            # what the app itself said
kubectl get svc -n fleetpulse               # the list of names that actually exist
```

Two things worth knowing about the last two:

- **`kubectl logs` on a crashed pod shows the *new* attempt, not the one that failed.** Add
  `--previous` to see the crash that actually matters.
- **A pod with more than one container needs `-c <container-name>`.** Guessing the name doesn't
  work; get the real ones with:
  ```powershell
  kubectl get pod <name> -n fleetpulse -o jsonpath='{.spec.initContainers[*].name}{"\n"}{.spec.containers[*].name}'
  ```

## The habit worth forming

Before applying anything, list the names side by side and check them against each other:

```powershell
kubectl get svc -n fleetpulse                          # names that exist
Select-String -Path infra\k8s\base\*.yml -Pattern "name:|nc -z|@.*:5432|redis://"   # names being referenced
```

Thirty seconds of this beats twenty minutes of `describe`.
