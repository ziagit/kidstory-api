---
title: Kidstory Story Model
emoji: 🐭
colorFrom: yellow
colorTo: pink
sdk: docker
app_port: 7860
pinned: false
---

# Kidstory story model

Serves [`ziaflutter/tinyllama-cat-mouse-story`](https://huggingface.co/ziaflutter/tinyllama-cat-mouse-story)
(a TinyLlama-1.1B story fine-tune) as an OpenAI-compatible chat endpoint using
[llama.cpp](https://github.com/ggml-org/llama.cpp), on the free CPU tier.

The Dockerfile converts the Hub's bf16 safetensors to a Q8_0 GGUF at build time,
so the first build takes several minutes; after that the container starts in
seconds. It sleeps after 48h without traffic and wakes on the next request.

## Endpoints

- `POST /v1/chat/completions` — OpenAI-style chat completions (what kidstory-api calls)
- `GET /health` — liveness (never requires the key)
- `/` — llama.cpp's built-in chat UI, handy for trying prompts by hand

## Secrets

| Name | Purpose |
|---|---|
| `LLAMA_API_KEY` | Optional. When set, every endpoint except `/health` requires `Authorization: Bearer <key>`. Use the same value as `STORY_MODEL_API_KEY` on the kidstory-api Space. |

## Calling it

```python
from huggingface_hub import InferenceClient

client = InferenceClient(base_url="https://ziaflutter-kidstory-model.hf.space/v1", api_key="<LLAMA_API_KEY>")
out = client.chat_completion(
    [{"role": "user", "content": "Write a short story about a cat and a mouse who become friends."}],
    max_tokens=400,
)
print(out.choices[0].message.content)
```
