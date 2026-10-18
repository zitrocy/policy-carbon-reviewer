# Policy-Compliant Carbon Reviewer

A pull-request style reviewer for Terraform plans that puts **policy first**:
a region with a cleaner grid is still the wrong region if it violates your
data residency or allowed-region policy.

> Code decides. Gemini explains.

**Live demo (Cloud Run):** <https://policy-carbon-reviewer-803314170442.asia-northeast1.run.app>

Built for the Google Cloud AI Builder Cup 2026 (Sustainability & Social
Impact track). The bundled policy and plans are **synthetic demo data** - the
"Japan data residency" policy is an example, not a claim about any real
regulation.

## The problem

Teams that want to lower the carbon footprint of their cloud workloads can
move to a region with a cleaner power grid. But regions are not freely
interchangeable: data residency rules, allowed-region lists, and latency
constraints remove options first. Existing tooling shows carbon impact in a
PR but leaves the policy check to humans, so "the greener option" can be
proposed even when it is not a legal option.

## What this does

Paste a Terraform plan JSON (`terraform show -json plan.tfplan`) and a policy
- either structured JSON, or the policy written in plain English. The review
runs as a pipeline of seven narrow agents:

0. **Policy interpreter** *(model)*. Optional. Reads a prose policy
   ("production data has to stay inside Japan") and proposes a structured one.
   Every region it returns is checked against the official region catalog; one
   invented region fails the whole interpretation rather than being quietly
   dropped, because a policy that silently lost a region is worse than no
   policy. There is no guessed fallback.
1. **Plan analyst.** Only `google_compute_instance` changes are reviewed;
   every other resource type is reported as unsupported instead of being
   silently skipped. Missing zones/regions are flagged, never guessed.
2. **Policy gate.** Each target region is checked against your allowed-region
   list and (optionally) a data residency jurisdiction. Any violation blocks
   the region - no matter how low its carbon intensity is.
3. **Carbon analyst.** Allowed regions only, ranked by **grid carbon
   intensity (gCO2eq/kWh)** with **CFE %** shown as a separate metric, from
   Google's official 2025 annual region data (source and year displayed in
   the UI). These are grid metrics, not measured workload emissions.
4. **Impact estimator.** Turns machine shapes into an annual **kgCO2eq**
   figure, so the review answers "how much does this matter" and not only
   "which region is cleaner". See [Estimating impact](#estimating-impact) for
   what that number is and is not.
5. **Review writer** *(model)*. Gemini writes the PR comment from
   code-verified facts only: the allowed candidate IDs, the computed numbers,
   and the block reasons.
6. **Verifier.** The response is validated back against those facts
   (recommended region must be an allowed candidate, required fields must be
   present). If validation fails or the model is unreachable, the UI falls
   back to a clearly labeled code-generated summary - never passed off as
   model output.

Every run returns an **agent trace** showing which stages ran, which were
deterministic, and which involved a model, so the architecture is inspectable
from the API rather than taken on trust.

## Three people read this review

The same review serves three readers who need different things from it, so the
UI has a **View as** selector. A role changes only what is on screen - it can
never change a decision, and every role sees the same underlying review.

- **Infrastructure operator** - about to merge the change. Sees the verdict,
  the estimated cost, and the comment to paste on the pull request.
- **Policy administrator** - owns the policy. Sees what the policy allowed and
  blocked, and the record of every review run against it.
- **Auditor (read-only)** - checking how the answer was produced. Sees the
  agent trace, the data provenance, the methodology, and the append-only
  record. Running a review is disabled in this view.

Two views support that:

- **Change reviews** is an append-only record of every review run in this
  browser: when it ran, the policy, the verdict, the estimated impact, and
  whether a model wrote the comment or a code-generated summary stood in. It
  is browser-local (`localStorage`) - a demo stand-in for the server-side
  audit log a real deployment would keep, and it never leaves the machine.
- **Methodology & limitations** puts the data source, the units, the estimate's
  assumptions, and the honest list of what this tool is *not* on screen rather
  than only in this file.

## Architecture

Two of the seven agents call a model. Both do language work - reading prose,
writing prose. Every decision a reviewer would be angry to see a model get
wrong is computed in code, and the verifier checks the model's output back
against those computed facts.

```
prose policy ----> [policy-interpreter]  MODEL
                        | proposed regions
                        v
                   verify against region catalog
                        | (reject on any invented region)
                        v
structured policy --+   +--- terraform plan JSON
                    |   |
                    v   v
                [plan-analyst]           code
                        |
                        v
                 [policy-gate]           code   <- carbon can never override
                        |  allowed regions only
                        v
                [carbon-analyst]         code   official 2025 region data
                        |  ranked by grid gCO2eq/kWh
                        v
              [impact-estimator]         code   annual kgCO2eq + assumptions
                        |  computed facts only
                        v
                [review-writer]          MODEL
                        |  proposed comment
                        v
                   [verifier]            code
                        |
          +-------------+-------------+
          v                           v
   verified PR comment        labeled code-generated
                              fallback summary
```

- `app/agents.py` - pipeline registry and execution trace
- `app/plan_parser.py` - Terraform plan parsing (google_compute_instance only)
- `app/policy.py` - deterministic policy gate
- `app/policy_interpreter.py` - prose policy -> structured policy, verified
- `app/carbon.py` - official region carbon data + comparison
- `app/impact.py` - annual kgCO2eq estimate with stated assumptions
- `app/gemini.py` - Gemini call, response schema, verification, fallback
- `app/reviewer.py` - orchestration
- `app/main.py` - FastAPI server + single-page UI
- `app/data/carbon_regions.json` - Google official 2025 annual data,
  retrieved 2026-10-18 from
  <https://cloud.google.com/sustainability/region-carbon>

## Estimating impact

The region table answers "which allowed region is cleanest". The impact
estimator answers the next question: how much does this change actually
matter, in kgCO2eq per year.

It is an **estimate from vCPU count**, not a measurement and not a bill.
Google does not publish per-instance power draw, so the machine shape is
turned into an assumed average draw. Every assumption travels with the
number, in the API response and in the PR comment:

- 7 W average draw per vCPU (a planning constant, not a vendor figure)
- 100% duty cycle - a VM declared in Terraform with no schedule is assumed
  to run all year
- PUE 1.1 for cooling and power-delivery overhead
- the official annual average grid intensity, not the marginal intensity at
  the hour the workload runs
- compute only: no storage, network, or embodied emissions

Three rules keep the number honest:

- **Unknown machine shapes produce no estimate.** A shape the parser does not
  recognize is reported as unestimated, never given a default.
- **Blocked regions are never costed as a saving.** If the policy gate blocks
  every candidate there is no approved destination, so only the current
  footprint is shown. Reporting "this move saves 117 kg" for a move the policy
  forbids is exactly the failure this tool exists to prevent.
- **Migrations and new resources are kept apart.** A plan that moves one VM to
  a cleaner region while creating another is not a saving of the difference.
  The delta covers only resources that exist on both sides of the change;
  newly created resources are totalled separately as an addition.

## Run locally

```bash
pip install -r requirements-dev.txt
export GEMINI_API_KEY=...   # optional; without it you get the labeled offline summary
uvicorn app.main:app --reload --port 8080
# open http://localhost:8080
```

## Tests

```bash
python -m pytest tests/ -q
```

126 tests cover the parser, the policy gate, the policy interpreter's
verification rules, the carbon comparison, the impact estimator's accounting
rules, the Gemini verification/fallback path, the agent trace, the
orchestration, and the API.

## Deploy to Cloud Run

```bash
gcloud run deploy policy-carbon-reviewer \
  --source . \
  --region asia-northeast1 \
  --allow-unauthenticated \
  --set-env-vars GEMINI_API_KEY=YOUR_KEY
```

`PORT` is honored automatically. No key is baked into the image; set
`GEMINI_API_KEY` (and optionally `GEMINI_MODEL`) as runtime env vars.
For a public demo, add rate limiting or restrict invocation - the API runs
one Gemini call per review.

## API

- `GET /` - demo UI
- `GET /api/samples` - the four demo scenarios (blocked / compare / unknown /
  prose policy)
- `GET /api/agents` - the pipeline: each agent, its engine, and its role
- `POST /api/review` - body `{"plan": {...}, "policy": {...}}`, or
  `{"plan": {...}, "policy_text": "production data must stay in Japan"}` to
  run the interpreter agent first. Responses carry `reviewed_at`,
  `agent_trace`, `impact`, `gate`, `comparison`, and `review`.
- `GET /api/carbon-data` - data source, year, and units
- `GET /api/healthz` - liveness + whether Gemini is configured

## Limits (on purpose)

- Reviews `google_compute_instance` only; other types are reported, not reviewed.
- The kgCO2eq figure is an estimate from vCPU count with stated assumptions,
  not a measurement, a bill, or a carbon accounting output.
- No real-time grid data and no cost modeling: regions are compared on annual
  averages.
- The policy interpreter needs `GEMINI_API_KEY`. Without one it refuses rather
  than guessing a policy; structured JSON still works offline.
- The role selector is a demo control, not authentication. It changes what is
  displayed, never what is computed, and grants no permissions.
- The change-review record lives in one browser's `localStorage`. It is not a
  shared audit log, does not survive clearing site data, and is absent in a
  private window - the review itself works regardless.
- Advisory output only. Nothing here changes infrastructure or real PRs.
- The policy you paste is verified as written; this is not a legal compliance
  certification service.

## Roadmap (post-MVP)

GitHub App integration that posts the comment to real PRs, additional
resource types (GKE, Cloud SQL), measured per-workload energy use in place of
the vCPU estimate, hourly grid intensity instead of annual averages, and a
server-side audit log with real identities behind the role views (today's
record is browser-local and the roles are a demo selector, not authentication).

## License

MIT. See [LICENSE](LICENSE).
