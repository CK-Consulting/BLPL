# Running models on your own hardware

Two machines, serving different jobs, for a reason that is about size rather
than preference: a 32 GB card holds several small specialised models at once, a
128 GB unified-memory box holds one large one, and those are different kinds of
useful.

| | x6 (RTX 5090, 32 GB) | AGX Thor (122 GB unified) |
|---|---|---|
| serves | several small models at once | one large model |
| via | Ollama, in the compose stack | vLLM, over the LAN |
| kinds | GGUF fine-tunes, a VLM | NVFP4 120B MoE |

## x6 — several models at once

Ollama rather than vLLM or NIM on this box, for reasons specific to it:

* It keeps several models resident and swaps on demand
  (`OLLAMA_MAX_LOADED_MODELS`). vLLM and NIM are one model per process, so three
  specialised models would mean three servers splitting 32 GB with every set of
  weights pinned whether or not anything is asking for them.
* Most community fine-tunes in this domain ship as GGUF, which is what
  llama.cpp reads and what neither of the others will load.
* It speaks the OpenAI API, which is what BLPL already routes to.

```
docker compose --profile local-models up -d ollama
```

Three models fit comfortably and stay resident together — measured, all on GPU:

| model | VRAM | generation |
|---|---|---|
| `qwen2.5vl:7b` | 9.6 GB | reads datasheet tables |
| `hf.co/21world/KiCAD-MCP-Qwen3.5-4B-GGUF` | 9.7 GB | 157 tok/s |
| `hf.co/Spidey106/electronics-assistant-gguf` | 4.8 GB | 426 tok/s |

26.1 GB of 32.6 GB with all three loaded.

**Model names are not guessable**, which is why Settings has a *list models*
button rather than a text box: Ollama serves everything it holds on one port, so
the port names the server and the string names the model, and the string is
`hf.co/21world/KiCAD-MCP-Qwen3.5-4B-GGUF:latest`.

**Two of the linked models are not what their names say.** Worth checking any
community fine-tune the same way before building on it:

* `Ornith-1.0-9B-Hardware-Expert-fp16` is **not fp16** — its `config.json`
  declares bitsandbytes NF4 4-bit. Ollama cannot load it at all.
* `Spidey106/electronics-assistant-gguf` ships a file named
  `Qwen2.5-3B-Instruct.Q4_K_M.gguf` — the stock base model's filename.

**None of them read images.** Vision is a separate model (`qwen2.5vl`), routed
through the `vision` task.

## Thor — one large model

Thor is **not** a DGX Spark: Thor is compute capability **11.0** (sm_110),
Spark's GB10 is **12.1** (sm_121), and NVFP4 kernels compile per-arch. That
looked like the risk. It is not — `vllm/vllm-openai:v0.20.0` (arm64) reports

```
archs: ['sm_80', 'sm_90', 'sm_100', 'sm_110', 'sm_120']
NVIDIA Thor sm_110 — 28.9 TFLOP/s bf16 (4096³ matmul)
```

so the stock image already carries kernels for this chip and needs no
Jetson-specific build.

**Two L4T differences do stand in the way, and both masquerade as the
architecture problem.**

*`--gpus` is rejected.* The hook answers "invoking the NVIDIA Container Runtime
Hook directly is not supported. Please use the NVIDIA Container Runtime". Use
`--runtime=nvidia`. The cookbook says `--gpus all` because it targets Spark.

*No driver library is injected.* Neither
`/etc/nvidia-container-runtime/host-files-for-container.d/drivers.csv` nor the
CDI spec at `/var/run/cdi/nvidia.yaml` mentions `libcuda` — `grep -c` returns 0
for both. The container therefore resolves `libcuda` to the CUDA **compat** stub
baked into the image, which is built for discrete GPUs (driver 580.95.05) and
cannot drive Tegra.

The symptom is what makes this expensive: there is no error. Torch reports

```
cuda available: False
archs: []
```

An empty arch list reads exactly like "this image has no kernels for your GPU",
which points at rebuilding for sm_110 — the one thing that was never wrong. The
fix is a bind mount:

```
-v $(readlink -f /usr/lib/aarch64-linux-gnu/libcuda.so.1):/usr/lib/aarch64-linux-gnu/libcuda.so.1:ro
-e LD_LIBRARY_PATH=/usr/lib/aarch64-linux-gnu:/usr/local/cuda/lib64
```

On this machine that symlink resolves to
`/opt/nvidia/l4t-gpu-libs/openrm/libcuda.so.1.1`.

Memory is not the constraint people expect. Only 8 of 88 layers are attention
(the rest are Mamba and MoE) with 2 KV heads, so the KV cache is **4 KB/token**
at fp8 — 16.4 GB for 1M tokens across 4 sequences, against roughly 67 GB of
weights.

Context defaults to 262144 here rather than 1M. The card supports 1M, but only
with `VLLM_ALLOW_LONG_MAX_MODEL_LEN=1` overriding the model's own declared
limit, and NVIDIA's published RULER numbers stop at 256k. Get it serving, then
raise `CTX`.

`--gpu-memory-utilization` is 0.80, not the cookbook's 0.90: Thor's memory is
*unified*, so that fraction is taken from the pool the OS is also living in.

vLLM authenticates natively (`--api-key`), so nothing needs to sit in front of
it for that.

## Getting large files onto the Thor

Two facts measured on this network, both worth knowing before waiting hours:

**Hugging Face shapes per connection.** One stream gets 3.5 MB/s; six get
60 MB/s aggregate. An 80 GB checkpoint is 6 hours single-threaded and about 25
minutes across six. Concurrency is the lever, not patience.

**The Thor's route to the internet corrupts data.** Repeated pulls die with
`tls: bad record MAC` — bytes altered in flight, caught only because TLS
authenticates them. Its LAN path to x6 runs at 195 MB/s cleanly. So large
artifacts are fetched on x6 and moved over the LAN:

```
# image (33 GB): about 3 minutes
docker save vllm/vllm-openai:v0.20.0 | ssh thor 'docker load'

# weights (80 GB): about 7 minutes
rsync -a --info=progress2 ~/nemotron-weights/ thor:~/nemotron/model/
```

Anything that must cross that link should be verified by size or checksum
afterwards. A truncated safetensors shard loads as a corrupt model rather than
failing loudly, which is the worst way for a bad download to present.
