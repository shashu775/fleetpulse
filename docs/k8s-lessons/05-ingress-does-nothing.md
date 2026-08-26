# 5. Everything is Running, but the website won't open

## What I saw

All pods healthy. `kubectl get ingress` looked perfectly normal:

```
NAME         CLASS   HOSTS              ADDRESS   PORTS   AGE
fleetpulse   nginx   fleetpulse.local             80      7m
```

And `http://fleetpulse.localhost/` in the browser: nothing. No error page, no timeout message worth
reading — just no connection.

## The one command that settled it

```powershell
netstat -ano | Select-String "LISTENING" | Select-String ":80 "
```

**Nothing was listening on port 80.** Not a DNS problem. Not a hosts-file problem. Not the app.
There was simply no door.

Meanwhile the site was serving fine *inside* the cluster:

```powershell
kubectl run test --rm -i --restart=Never --image=busybox:1.36 -n fleetpulse -- wget -qO- http://web-service/healthz
# ok
```

So: the app worked. I had no way in from Windows.

## An Ingress is a note, not a doorman

This is the part that isn't obvious.

**An Ingress object doesn't route anything.** It's a written instruction — *"visitors asking for
fleetpulse.test should go to the web team"* — taped to a desk. Something has to actually **read**
it and walk people over. That something is an **ingress controller**, a real program running in the
cluster that opens port 80 and follows the instructions.

Minikube gives you one with `minikube addons enable ingress`. **Docker Desktop ships none.**

```powershell
kubectl get ingressclass
# No resources found     ← nobody is reading your Ingress
```

That empty result is the whole diagnosis. And notice the failure mode:

> Kubernetes accepted the Ingress, stored it, and shows it in `kubectl get`. It looks deployed. It
> does nothing. **No error is ever produced.**

The tell is the **empty `ADDRESS` column**. A working Ingress has an address there. Blank means
nobody has claimed it.

## The way in: port-forward

For local development this is simpler than an ingress controller, and it always works:

```powershell
kubectl port-forward -n fleetpulse svc/web-service 8080:80
```

Leave it running, open **http://localhost:8080/**. No hostname, no hosts file, no controller.

It's a temporary tunnel from your laptop straight to the Service — it dies when you Ctrl-C.

## Why `fleetpulse.localhost` was never going to work

Three separate reasons, any one of them fatal:

1. **Nothing was listening on port 80** (no controller).
2. **`fleetpulse.localhost` is the Docker Compose name.** It worked under Compose because Compose
   published `80:80` on the host. Kubernetes publishes nothing by default.
3. **The Ingress said `fleetpulse.local`** — a different name again.

Three names for one project is how you lose an afternoon:

| Where | Hostname |
|---|---|
| Docker Compose | `fleetpulse.localhost` |
| Kubernetes | `fleetpulse.test` |
| ❌ never use | `fleetpulse.local` |

> **Worth checking before you blame yourself:** `fleetpulse.local` was never in the manifests.
> Both `base_bkp/08-ingress.yaml` and the committed version say `fleetpulse.test`; the `.local`
> spelling was introduced later by hand. If you have a memory of `.local` working, check *which
> cluster* — `kubectl config get-contexts`. Other projects on the kubeadm cluster
> (`argocd.local`, `task-manager.local` → `192.168.78.x`) do use `.local` and do have a controller.
> Same laptop, different cluster, different rules.

**Avoid `.local`.** It's reserved for Bonjour/mDNS (printers, Chromecasts), so those lookups can
race or bypass your hosts file and fail intermittently — the worst kind of bug.

**`.localhost` is special:** browsers resolve it themselves, no setup. That's why Compose "just
works" in Chrome but the same URL fails from `curl` or PowerShell.

**`.test` is not special:** it needs a hosts-file entry. That's a feature — keeping the two stacks on
different names means you always know which one you're looking at.

## How this was resolved

A controller is now vendored into the repo at
[`infra/k8s/base/00-ingress-nginx-controller.yml`](../../infra/k8s/base/00-ingress-nginx-controller.yml)
(ingress-nginx `controller-v1.13.9`), so it installs with everything else:

```powershell
kubectl apply -f infra/k8s/base/00-ingress-nginx-controller.yml
kubectl wait --namespace ingress-nginx --for=condition=ready pod `
  --selector=app.kubernetes.io/component=controller --timeout=180s
```

**Why the "cloud" variant and not "baremetal":** cloud creates a `LoadBalancer` Service, and Docker
Desktop implements those by binding port 80 on the host directly. The baremetal variant uses a
NodePort in the 30000+ range, which would have put the site on `fleetpulse.test:31234`.

Then, as **Administrator**:

```powershell
Add-Content -Path "$env:SystemRoot\System32\drivers\etc\hosts" -Value "127.0.0.1 fleetpulse.test"
```

### How you can tell it worked

Before and after, the difference is visible in two places:

```
kubectl get ingressclass
  before:  No resources found
  after:   nginx   k8s.io/ingress-nginx

kubectl get ingress -n fleetpulse
  before:  fleetpulse   nginx   fleetpulse.test             80     ← ADDRESS blank
  after:   fleetpulse   nginx   fleetpulse.test   localhost  80    ← claimed
```

**Test routing before touching DNS.** An explicit `Host:` header proves Kubernetes works without
involving the hosts file at all — so if this passes and the browser still fails, you know the
problem is DNS, not the cluster:

```powershell
curl.exe -s -o NUL -w "%{http_code}`n" -H "Host: fleetpulse.test" http://127.0.0.1/
```

`200` = working. `404` = the host rule doesn't match. `503` = the backend Service name is wrong.

### ⚠️ One consequence to know about

The LoadBalancer binds **`0.0.0.0:80`**, not `127.0.0.1:80`:

```
TCP    0.0.0.0:80    0.0.0.0:0    LISTENING
```

That means FleetPulse — including the admin console, which can read every parcel and create
runsheets — is now reachable **by anyone on your network**, and the app has no authentication of
any kind. Docker Compose deliberately avoids this by binding every port to `127.0.0.1` (see the
port-binding section in `CLAUDE.md`); the Kubernetes path gives that up.

Fine on a trusted home network. Do not leave it running on café or conference wifi. `kubectl delete
-f infra/k8s/base/00-ingress-nginx-controller.yml` closes the door again, and port-forward
(`127.0.0.1` only) remains the safer option when you're not on a network you control.

## Two more name traps in the Ingress itself

Mine pointed at `service: name: web`. There is no Service called `web` — the Deployment is `web`,
the **Service** is `web-service`. An Ingress always points at a Service, never a Deployment or a
pod. With a controller installed this would have been a 503 with healthy pods behind it.

## Worth knowing: this won't work on our EKS either

`infra/terraform/environments/dev/main.tf` deliberately skips the AWS Load Balancer Controller,
because an Ingress there would create a real load balancer at ~$16/month. So on EKS as configured,
an Ingress is inert for the same reason it is here — just for a cost decision rather than a
missing add-on.

---

**How to remember it:** an Ingress is a **note**, not a doorman. No controller = no traffic, and no
error either. Empty `ADDRESS` column is the tell.

**Check it in 5 seconds:**
```powershell
kubectl get ingressclass          # empty = nothing will ever read your Ingress
```
