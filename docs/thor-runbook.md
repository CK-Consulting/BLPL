# Running the model on a Jetson AGX Thor

BLPL itself does not belong on the Thor. The model does. This is why, and what
to put there.

## Why the split

`kicad/kicad:10.0.0` publishes **linux/amd64 only** — there is no arm64 manifest,
which is why `app/docker-compose.yml` pins `platform: linux/amd64`. The Thor is
ARM64, so running BLPL there means the entire pipeline executes under QEMU
emulation. That is not a theoretical penalty: the same emulation on an Apple
Silicon dev machine turns a 25-second test suite into two and a half minutes, and
stage 6 and 7 are heavier than the test suite.

Meanwhile nothing in stages 0–8 touches a GPU. The pipeline calls
`kicad-cli pcb drc`, `kicad-cli sch erc`, and the export subcommands — never
`kicad-cli pcb render`, and KiCad's raytracer is CPU anyway. It is text parsing,
geometry assembly, rule checking and file writing.

So:

| Machine | Runs | Because |
|---|---|---|
| amd64 server (72–96 threads) | BLPL: API, workers, Postgres | Native KiCad, and the worker pool turns threads into concurrent runs |
| Jetson AGX Thor | The model | The GPU and unified memory are decisive here and nowhere else |

## What to run on the Thor

### The runner

**Start with Ollama.** BLPL treats it as a first-class provider with no API key,
so a user selects "Ollama", pastes the base URL, and is done. On Jetson the
reliable path is NVIDIA's `jetson-containers`, which packages a CUDA-enabled
build against the installed JetPack rather than leaving you to match versions:

```bash
git clone https://github.com/dusty-nv/jetson-containers
cd jetson-containers && ./install.sh
jetson-containers run --name ollama $(autotag ollama)
```

**Move to vLLM when concurrency starts to matter.** Ollama serves requests
essentially one at a time; vLLM batches them. With several people running stages
at once — which is exactly what the worker pool now allows — that is the
difference between a queue and a fleet. vLLM exposes an OpenAI-compatible API, so
in BLPL it is an `openai-compatible` endpoint with a base URL rather than a new
integration. Jetson support is less turnkey than Ollama's; treat it as the second
step, not the first.

### Bind address, or nothing will reach it

Ollama listens on `127.0.0.1` by default, which means the BLPL box cannot see it.
This is the single most common reason a local model "does not work":

```bash
OLLAMA_HOST=0.0.0.0:11434 ollama serve
```

Then on the BLPL server, in `app/.env`:

```bash
OLLAMA_HOST=http://<thor-address>:11434
```

### Ollama has no authentication

None. Anyone who can reach that port can use the GPU, list the models, and pull
new ones. Do not put it on a network you do not control. Either keep it on a
private link between the two machines, or front it with something that requires a
credential — in which case configure it in BLPL as an `openai-compatible`
endpoint with a key rather than as Ollama, since that is the shape that carries
one.

## Which model

Pick by the task, because BLPL routes tasks separately — that is the point of the
endpoint registry, and it is what makes a local model useful even when it cannot
do everything.

| Task | What it actually does | What that needs |
|---|---|---|
| `stage0` | Markdown → `design_artifact.json`, schema-validated | Instruction-following and reliable JSON. A mid-size model is enough, and it runs on every document you edit, so latency matters more than depth |
| `stage1` | Component resolution and connector synthesis → BOM | The hardest reasoning in the pipeline. Worth the largest model you can hold |
| `datasheet_vision` | Reads PDF pages | **Vision is mandatory.** A model without it silently reads nothing — BLPL refuses to route this task to an endpoint that has not declared `vision = true`, precisely because that failure is invisible |
| `review_panel` | Every routed endpoint reviews the same evidence | Diversity beats size. A local model alongside a cloud one produces disagreements worth reading |
| `chat` | Interactive design conversation | First-token latency. The smallest model you find acceptable |

With 128 GB of unified memory a 70B-class model at 4-bit quantisation
(~40 GB) fits with room for context, so capacity is unlikely to be the binding
constraint — throughput will be.

**On specific model names:** they date faster than anything else in this
document, so treat the criteria above as the durable part. As of this writing the
shapes that fit are a Qwen2.5-VL variant for `datasheet_vision`, a 70B-class
instruct model for `stage1`, and something small and quick for `stage0` and
`chat`. Check what is current before pulling — this list will be wrong before the
rest of the page is.

## A sensible starting arrangement

Not everything has to be local. The routing exists so it does not have to be:

```
chat              → local, small        (latency)
stage0            → local, small        (runs constantly, cheap)
stage1            → cloud               (hardest reasoning; local later)
datasheet_vision  → cloud, or local VL  (vision is mandatory either way)
review_panel      → cloud + local       (disagreement is the product)
```

That gives the cost control and the mixture-of-providers effect without betting
the whole pipeline on one machine. Move tasks to the Thor as you confirm the
local model handles them — per task, reversibly, in Settings.

## Verifying it

From the BLPL server, before configuring anything in the app:

```bash
curl http://<thor-address>:11434/api/tags        # models the Thor is serving
```

Then in BLPL: Settings → add an endpoint of kind `ollama` with that base URL,
route one task to it, and run that stage. Watch it in the run panel — a model
that is reachable but too slow looks exactly like one that is not responding, and
the progress panel is what tells them apart.
