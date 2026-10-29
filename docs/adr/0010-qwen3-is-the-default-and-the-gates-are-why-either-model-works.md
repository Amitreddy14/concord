# ADR 0010: qwen3:8b is the default, and the gates are why either model is usable

- **Status:** accepted
- **Date:** 2026-10-29
- **Phase:** 8
- **Deciders:** Amit Reddy

## Context

ADR 0003 fixed local inference and an 8B-class ceiling, and deferred the model choice to a
measurement in Phase 8. The roadmap asks for pass rate, latency, tokens and VRAM headroom across
the local shortlist on identical fixtures, with the winner recorded here.

`DEFAULT_SHORTLIST` in the benchmark names four models. Three were measured: `llama3.1:8b`,
`qwen3:8b` and `granite3.1-dense:8b`, all Q4 on an RTX 4060 with 8GB of VRAM.

`mistral-nemo:12b` was not run. At Q4 it is roughly 7GB of weights against an 8GB card, which
leaves too little for the KV cache at the 2.8k prompts these nodes build, and ADR 0003 already
sets the ceiling at 8B-class for that reason. It stays in the shortlist constant because the
constant is a list of candidates and a reader should see what was considered — but this ADR
carries no measurement of it, and nothing here should be read as one.

`granite3.1-dense:8b` was eliminated before any quality measurement: the build available through
Ollama does not support tool calling, and three of the graph's nodes are tool callers. That is an
architectural exclusion rather than a quality judgement — the model is not weaker here, it cannot
participate. It appears in the benchmark table as an unavailable row with that reason, because a
quietly missing row reads as a model that lost rather than one that was never tried.

### What the measurement actually measured

The benchmark ran five times over two scenarios (S01, a policy question with no mutation; S07, a
settlement delay behind an active incident) with ten declared assertions. The numbers moved a long
way between runs:

| Round | llama3.1:8b | qwen3:8b | What changed between rounds |
|---|---|---|---|
| 1 | 5/10 | 5/10 | — |
| 2 | 5/10 | 9/10 | The output contract named the `citations` field and where its ids come from |
| 3 | 9/10 | 8/10 | Dates rendered at the boundary; incident statuses classified against the contract |
| 4 | 9/10 | 8/10 | `unsupported_claims` disambiguated — and the instruction grew enough to harm both models |
| 5 | **9/10** | **10/10** | Instruction tightened from 602 to 485 tokens |

Not one of those movements came from the models. Every one is traceable to a defect in the prompt
or in the harness:

- Both models drafted correct replies and returned `citations: []`. The instruction said to cite;
  it never said the ids go in a field, which field, where to find them, or that an uncited reply
  is discarded.
- Dates reached the prompt as `2026-10-17T10:00:00Z` while amounts reached it through
  `Money.format()`. The model was the thing turning a timestamp into a sentence, which ADR 0004
  says the system does.
- The instruction required a passage citation for *every* factual claim, which made the merchant's
  own settlement date unstateable — it comes from the systems of record and has no passage id.
  qwen3 did the careful thing and omitted the date.
- `incident_cited_only_if_active` accepted two statuses `contracts/knowledge.yaml` does not define
  and omitted two it does. llama3.1 was marked wrong for correctly citing an `identified` incident.
- qwen3 listed the merchant's own sentence from the ticket under `unsupported_claims`, and a
  complete, correct, fully grounded reply was replaced by a refusal.

Two of those five are harness defects, not agent defects. A benchmark is also software, and this
one was wrong about the contract twice.

The identical-failure signature is what made each one findable. Two unrelated 8B models failing
the same assertion in the same way is evidence about the instruction; models failing differently
is evidence about the models. That distinction did more work than any individual number.

### The measurements, final round

| | llama3.1:8b | qwen3:8b |
|---|---|---|
| Assertions passed | 9/10 | 10/10 |
| Median run | 3,833 ms | 19,907 ms |
| Slowest run | 3,953 ms | 22,112 ms |
| Prompt tokens | 2,656 | 2,829 |
| Completion tokens | 278 | 1,579 |
| VRAM headroom | 1,668 MiB | 1,069 MiB |

The latency gap is not a tokens-per-second gap. qwen3 emits five to eight times the completion
tokens because it reasons before answering, and that is also where its accuracy comes from. The
two are the same property measured twice.

### The difference that outlasted the prompt fixes

llama3.1's remaining failure is the cleanest result in the exercise. The expected settlement date
is in the facts block. The instruction says that if the merchant asked about a date, the answer
goes in the reply. qwen3 states it. llama3.1 writes a fluent, correct, grounded reply and leaves
it out. Nothing is left to clarify — this is the model.

The two models also fail in different *kinds*, which matters more than one assertion:

- **llama3.1 fails by omission.** It drops a fact it was given. The reply is true and incomplete.
- **qwen3 fails by invention, under instruction pressure.** When the instruction grew the
  adjudication vocabulary of round 4, it drafted a reply about a defective product into a
  settlement ticket and named `shipment_record_12345` and `delivery_receipt_67890`. There is no
  product in S07.

An omission is visible to a reviewer reading the draft. An invented record is not: it reads as
specific, authoritative detail, and it is the more plausible-sounding of the two. That asymmetry,
not the pass rate, is the substance of this decision.

## Decision

Default to `qwen3:8b`. Record `llama3.1:8b` as the supported low-latency alternative. Treat the
deterministic gates as a precondition for either, not an enhancement.

`LLM_MODEL_PRIMARY` already defaults to `qwen3:8b`, so this changes no configuration — it
replaces a provisional default with a measured one, which is the point of having written it down.
`LLM_MODEL_TRIAGE` continues to default to the primary model; the split ADR 0003 left open stays
closed, because triage is the one node where both models now agree and there is nothing to buy.

The gate clause is the operative half. Every unsafe output seen across five rounds was stopped by
a deterministic check and not by model quality:

- Fabricated citations were caught by `verify`'s unknown-passage check.
- The invented shipment records were caught because they were also cited — and that near miss
  produced outbound entity re-validation (ADR 0005 rule 3 applied to what the model emits, not
  only to what it reads). Named in prose and omitted from the citations array, two invented
  records would have reached a merchant.
- Not one approval gate was skipped by either model, because no model is asked. The tier table
  decides (ADR 0004).

So the honest form of this decision is: at 8B, the model is not the variable that determines
whether this system is safe to deploy. It determines how often a correct reply has to be thrown
away. Both candidates produced an unsendable draft at some point; neither produced an unsafe
*action*, because actions do not route through the model's judgement.

## Consequences

**Accepted costs.**

- A resolution takes roughly 20 seconds of model time against llama3.1's 4. The API has been
  asynchronous since Phase 3 (ADR 0003), so this lands as queue depth rather than as a blocked
  request. At merchant-support volumes that is acceptable; it would not be for a chat surface.
- 1,069 MiB of VRAM headroom is enough on an idle 4060 and is not much. A second resident model
  does not fit, which is a further reason the triage split stays closed.
- qwen3's reasoning tokens are not free in the cost model. `packages/shared/cost` prices them,
  and `estimated_hosted_cost` on this profile is the number to show anyone proposing a hosted
  equivalent.
- Choosing the model that fails by invention means accepting the failure mode that is harder for
  a human reviewer to catch. This is only defensible with the entity check in place, which is why
  it is written into the decision rather than listed as a nice-to-have.

**Unlocked.**

- A reviewer can read `evals/reports/benchmark.json` and see what each model cited, what it
  drafted, and what was sent instead — the diagnostics exist because three consecutive diagnoses
  stalled for want of them.
- Swapping the model is configuration. The benchmark is the regression test for doing so.

**What would reverse this.**

- llama3.1 stating the facts it is given, under an instruction no longer than the current one.
  It is 4× faster and fails more visibly; those are real advantages and only the omission costs
  it the default.
- A tool-calling `granite3.1-dense` build, which would make the shortlist three again.
- Headroom pressure from anything else on the box, which would favour the model that emits a
  fifth of the tokens.

### What this measurement does not support

Two scenarios, ten assertions, one run per model per round, temperature 0 with a fixed seed. That
is a sample small enough that 10/10 against 9/10 is one assertion, and it is not a statistically
meaningful separation. The claims this ADR rests on are the ones the sample can carry: granite
cannot call tools; qwen3 states a fact in the records that llama3.1 omits, reproducibly across
rounds 3, 4 and 5; qwen3 fabricated records under a longer instruction and llama3.1 did not; the
latency and token profiles differ by a factor large enough that no sample size is in question.

The pass rates themselves are the weakest evidence here. Their value was diagnostic — they moved
when the prompt was wrong — and a ranking derived from them should be re-measured on the full
twelve-scenario suite with repeats before anyone treats it as a quality ordering.

## Alternatives considered

**llama3.1:8b as the default.** Tempting on every axis except the one that matters: it is faster,
leaner, and fails in the direction a human catches. It omits a fact it was explicitly instructed
to state and explicitly handed. For a support desk whose purpose is answering the question asked,
an answer that leaves out the date the merchant is waiting for is the product failing quietly.
Recorded as supported, and the gap to close is a narrow one.

**Split triage onto a smaller model.** ADR 0003 left this open and Phase 8 was to measure it. Both
models classify all seven intents correctly since the intents were defined; there is no accuracy
to recover, VRAM will not hold a second resident model, and swapping models between nodes thrashes
badly enough to cost more than it saves. Closed.

**A larger model at heavier quantisation.** A 14B at Q3 fits in 8GB on paper. It was not measured,
because the failure modes that mattered in five rounds were instruction-following and fabrication
rather than knowledge, and Q3 degrades instruction-following first. Worth revisiting only with a
specific hypothesis.

**A hosted frontier model.** Would resolve both failure modes and contradicts ADR 0003, which
exists because merchant support tickets carry cardholder-adjacent and KYC data. The cost model
prices this comparison for the conversation where a compliance team is in the room.
