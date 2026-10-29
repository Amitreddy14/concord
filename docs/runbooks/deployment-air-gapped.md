# Runbook: deploying into an air-gapped environment

The reason this document exists is ADR 0003. Merchant support tickets carry cardholder-adjacent
data, settlement figures and KYC status, and a system that cannot run without egress cannot be
deployed where a compliance team says no egress. Local inference is the architectural answer; this
is the operational half of it.

"Air-gapped" here means: no route to the public internet from the host that runs this. Internal
DNS, an internal registry and the four systems of record are reachable. If your environment
allows an outbound proxy to a package mirror, you have an easier problem than this document
assumes and can skip to step 5.

---

## What has to cross the gap

Five artefacts. Everything else is in the repository.

| Artefact | How to get it | Rough size |
| --- | --- | --- |
| The repository, at a tag | `git bundle create concord.bundle --all` | tens of MB |
| Python wheels for every dependency | `make freeze` then `pip download -r requirements.lock -d wheels/` | ~400 MB |
| Node modules for the console | `npm ci --prefix apps/console` then tar `apps/console/node_modules` | ~300 MB |
| Container images | `docker save` for each image in `infra/docker-compose.yml` | ~2 GB |
| Model weights | `ollama pull` on a connected host, then copy `~/.ollama/models` | ~6 GB |

The model weights dominate, and they are the artefact people forget. `qwen3:8b` at Q4 plus
`nomic-embed-text` is around 6 GB; budget for both, because retrieval without the embedding model
fails in a way that looks like a corpus problem.

Pull the images by digest rather than tag when you save them. A tag is a moving pointer, and the
point of this exercise is that the inside of the gap can be rebuilt from what you carried in.

```bash
# On the connected host
docker compose -f infra/docker-compose.yml pull
docker save pgvector/pgvector:pg16 jaegertracing/all-in-one:1.60 -o images.tar
```

`crm`, `payments`, `ticketing` and `knowledge` are built from `infra/mocks.Dockerfile` rather than
pulled — they are the mock systems of record, and in a real deployment they are replaced by the
real ones, which is the next section.

---

## What you are actually replacing

The four mock services exist so the scenarios can run (ADR 0002). In a real deployment they are
the customer's own systems, and the substitution is configuration:

```
CRM_BASE_URL         → the real CRM
PAYMENTS_BASE_URL    → the real payments platform
TICKETING_BASE_URL   → the real ticketing system
KNOWLEDGE_BASE_URL   → the real knowledge base, or keep the mock and point it at a corpus
```

The contracts in `contracts/` are the interface. A real system that does not match them needs an
adapter, and the adapter belongs in front of the URL rather than inside `packages/` — the same
rule that keeps a tenant out of the code keeps a customer's API quirks out of it.

Two things to check before assuming a real system will drop in:

**Idempotency.** `packages/tools/keys` derives a key from `hash(run_id, node, tool, args)` and
sends it on every mutating call. If the real payments platform ignores the header, a retry after an
indeterminate failure can double-charge, and S09 will not catch it because the mock honours the
header. Confirm the real one does before enabling retries.

**Problem details.** The error taxonomy reads RFC 9457 problem documents. A system returning bare
500s with HTML bodies degrades every error into the status-class fallback, which classifies more
things as `FATAL` than it should — survivable, but it turns retryable failures into escalations.

---

## 1. Carry it in and restore

```bash
git clone concord.bundle concord && cd concord && git checkout <tag>
docker load -i images.tar
cp -r models/ ~/.ollama/models/
tar -xzf console-modules.tgz
```

## 2. Install Python dependencies from the wheel directory

Dependencies are declared in `pyproject.toml`, not in a committed lock file — version floors
rather than pins, which is right for development and wrong for carrying across a gap. `make
freeze` on the connected host, at the tag you are deploying, writes `requirements.lock` from a
venv known to pass `make check`. Carry that file with the wheels.

```bash
python3 -m venv .venv
.venv/bin/pip install --no-index --find-links wheels/ -r requirements.lock
.venv/bin/pip install --no-index --no-deps --editable .
```

Three things about those commands.

`--no-index` is the one that matters. Without it a single missing wheel sends pip to PyPI, it
hangs until the connection times out, and the failure reads like a broken package rather than a
missing file.

The second install is the project itself, which the lock file excludes because it is editable.
`--no-deps` is what stops pip resolving dependencies it has already installed and reaching for
the network to confirm them.

And do not use `make install` here. It is the connected-host path and installs from the index.

## 3. Configure

```bash
cp .env.example .env
```

Change `POSTGRES_PASSWORD` — the example value is `change-me` and it means it. Set
`LLM_BASE_URL` to wherever Ollama is listening; `host.docker.internal` works on Docker Desktop and
does not exist on Linux, where you want the host's bridge address or `--network host`.

Set `OTEL_EXPORTER_OTLP_ENDPOINT` to your own collector or leave the Jaeger container in place.
Traces stay inside the gap either way, but check this before a demo: an unreachable collector adds
a connection timeout to every span flush.

## 4. Bring it up

```bash
make up          # postgres, the four systems of record, jaeger
make migrate
make doctor      # verifies inference and measures decode throughput
```

`make doctor` is the one to read. It confirms Ollama is reachable, the models are resident, and
reports tokens per second — and that number is the one to capture before anyone asks why a
resolution takes twenty seconds. ADR 0010 has the reference figures from an RTX 4060.

Do **not** run `make bootstrap` here. It calls `make models`, which runs `ollama pull`, which
needs the network you do not have.

## 5. Prove it works

```bash
make check       # lint, types, contracts, tests, the scenario suite, the console
make demo        # the narrated walkthrough
```

`make check` is the acceptance test for the deployment, not just for the code. It runs the twelve
scenarios against the in-process stand-in, so a green suite here says the Python environment,
the contracts and the policy engine are intact. It does **not** touch the model or the services
over the network — that is `make demo MODEL=<name>` and a real run against the API.

If `make check` passes and a real run fails, the fault is in the four URLs, the model, or the
network between them. That is a useful halving.

---

## Operating it

**The approval sweep must run.** LangGraph does not expire a frozen thread (ADR 0001), so
`ApprovalStore.sweep_expired` is ours to call. Nothing in this repository schedules it. A paused
run whose approval is never swept waits forever, and the merchant is never told — which is the
failure `approval_outcome_reached_the_merchant` exists to catch in a run that *was* swept. Put it
on a timer at a fraction of the shortest tenant's `expires_after_hours`.

**The dead-letter queue needs a human.** `packages/agent/deadletter` refuses to replay a letter
whose upstream effect is unknown, which is correct and means those letters accumulate until
someone reconciles them by hand. Nobody is alerted. Decide who looks at it and how often before
the first incident, not during one.

**Model residency.** `LLM_KEEP_ALIVE=30m` avoids VRAM reload thrash. On a shared GPU this fights
with whatever else is resident, and the symptom is a p99 latency that looks like a model problem
and is a scheduling one.

---

## What this runbook does not cover

**Secret management.** `.env` on disk is adequate for a single-host deployment and is not secret
management. Nothing here reads from a vault.

**Horizontal scale.** One process, one resident model, one tenant (see the onboarding runbook).
Two API replicas against one Postgres would work for intake; the checkpointer and the approval
index have not been tested under concurrent writers from separate processes.

**Backup and restore of the checkpointer.** A paused approval lives in the LangGraph checkpoint,
in Postgres. The database backup is therefore also the backup of every in-flight human decision,
and restoring to a point before an approval was granted will resume a run that has already acted.
Nothing detects that. It is the sharpest operational edge in the system and it is unaddressed.

**Upgrading with runs in flight.** A paused run resumes into whatever code is deployed when the
reviewer answers. If the node graph changed shape between the pause and the resume, the behaviour
is undefined — the checkpoint holds state for a graph that no longer exists. Drain before
upgrading, or accept that paused runs may fail on resume.
