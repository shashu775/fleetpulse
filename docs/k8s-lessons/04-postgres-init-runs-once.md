# 4. Postgres sets itself up exactly once, and then never again

This is the most expensive trap in the project, because nothing looks broken when you hit it.

## The rule

`db/init.sql` — the file that creates the schemas and tables — is run by the Postgres image **only
when the data folder is completely empty.**

Start Postgres once and it remembers forever. After that:

- editing `init.sql` changes nothing
- changing `POSTGRES_PASSWORD` changes nothing
- changing `POSTGRES_USER` changes nothing

They are all **first-boot-only** settings. The container reads them, builds the database, and from
then on the data folder is the source of truth.

## How it bit me

The Secret said the user was `fleetpulse`. An older version of the StatefulSet had created the
database as `fleetadmin`. Postgres came up perfectly healthy, and then:

```
FATAL:  role "fleetadmin" does not exist
```

...every five seconds. The manifest and the actual database disagreed, and **the manifest loses** —
the database was built before I changed the file.

## Wiping it properly (the part people get wrong)

Deleting the pod does nothing. Deleting the StatefulSet does nothing either. The data lives in a
**PersistentVolumeClaim**, which deliberately outlives both:

```powershell
kubectl delete statefulset postgres -n fleetpulse
kubectl delete pvc data-postgres-0 -n fleetpulse    # ← the line everyone forgets
```

Skip the second command and Postgres comes back with the *old* schema and the *old* password, and
you conclude your fix didn't work.

Deleting the whole namespace does remove the PVC — but it also removes the hand-made ConfigMap
(see [02](02-containercreating-forever.md)), so you have to recreate that before Postgres starts.

The Compose equivalent of all this is a single `docker compose down -v`.

> ⚠️ On EKS that PVC is a real EBS volume. Deleting a cluster without deleting PVCs leaves the
> volume behind, **still billing**, invisible unless you go looking.

## Changing the password on a live database

Don't wipe data just to rotate a password. Change it in place:

```powershell
kubectl exec -n fleetpulse postgres-0 -- psql -U fleetpulse -d fleetpulse -c "ALTER USER fleetpulse WITH PASSWORD 'new-password';"
```

Then update the Secret and restart the apps.

## What `PGDATA` is for, and why to set it now

You may see this in the StatefulSet:

```yaml
- name: PGDATA
  value: /var/lib/postgresql/data/pgdata     # note: a SUBfolder of the mount
```

Postgres refuses to set itself up in a folder that isn't empty. On real cloud disks (AWS EBS and
most cloud storage), formatting creates a hidden `lost+found` folder — so the folder is *not* empty,
and Postgres gives up:

```
initdb: error: directory "/var/lib/postgresql/data" exists but is not empty
```

Pointing `PGDATA` at a subfolder dodges this: `pgdata/` is created fresh and empty, `lost+found`
sits harmlessly next to it.

**Docker Desktop doesn't need this** — its storage is a plain folder with no `lost+found`. But set
it now anyway, because:

> If Postgres sets itself up *without* `PGDATA` and you add it later, it looks in the new empty
> subfolder, sees no database, and **builds a brand new empty one**. Your data appears to have
> vanished. It's actually still there, one folder up, but the app can't see it.

Decide before the first successful boot. After that, changing it is a data-migration job.

⚠️ Also watch the spelling — the variable is `PGDATA`. I originally had `postgresData`, which
Postgres ignores completely. It looked like protection I didn't have.

## Bonus: liveness vs readiness on a database

The probe was originally a **liveness** probe. That's the wrong choice:

- **liveness** failing → Kubernetes **restarts** the pod
- **readiness** failing → Kubernetes just **stops sending it traffic**

A database recovering after an unclean shutdown can be unresponsive for a while. That's exactly when
restarting it is the worst possible move — you interrupt the recovery and start it over.

For databases: **readiness yes, liveness no.**

---

**How to remember it:** the database is built **once**, on an empty folder. After that the folder
wins and your YAML is just a suggestion.

**Check it in 5 seconds:**
```powershell
kubectl exec -n fleetpulse postgres-0 -- psql -U fleetpulse -d fleetpulse -c "\dt consignment.*"
```
Tables listed = `init.sql` ran. Empty = it never did, and you need a real wipe.
