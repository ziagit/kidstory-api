# Hugging Face Space that serves ziaflutter/tinyllama-cat-mouse-story behind an
# OpenAI-compatible /v1/chat/completions endpoint, on the free CPU tier.
#
# The model is pushed to the Hub as bf16 safetensors, which nothing free will
# serve directly. This image pulls it at build time, converts it to a Q8_0 GGUF
# (~1.2 GB, near-lossless for a 1.1B model, several times faster than bf16 on
# CPU) and runs llama.cpp's server on it. kidstory-api talks to it through
# huggingface_hub's InferenceClient(base_url=...).
FROM ghcr.io/ggml-org/llama.cpp:full

ARG MODEL_REPO=ziaflutter/tinyllama-cat-mouse-story
ENV MODEL_PATH=/models/model.q8_0.gguf

# Download + convert in one layer so the 2.2 GB safetensors never lands in the
# final image. convert_hf_to_gguf.py writes Q8_0 directly, no separate quantize.
RUN mkdir -p /models \
 && python3 -c "from huggingface_hub import snapshot_download; snapshot_download('${MODEL_REPO}', local_dir='/models/hf')" \
 && python3 /app/convert_hf_to_gguf.py /models/hf --outfile "${MODEL_PATH}" --outtype q8_0 \
 && rm -rf /models/hf /root/.cache/huggingface

# Spaces run the container as uid 1000 and route traffic to $PORT (7860).
RUN useradd -m -u 1000 user && chown -R user:user /models
USER user

# LLAMA_API_KEY, when set as a Space secret, makes every endpoint except
# /health require `Authorization: Bearer <key>` — set the same value as
# STORY_MODEL_API_KEY on the kidstory-api Space.
ENV LLAMA_ARG_HOST=0.0.0.0 \
    LLAMA_ARG_PORT=7860 \
    LLAMA_ARG_CTX_SIZE=2048 \
    LLAMA_ARG_N_PARALLEL=1 \
    LLAMA_ARG_ALIAS=kidstory-story-model

EXPOSE 7860
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s \
  CMD curl -fsS http://127.0.0.1:7860/health || exit 1

# --jinja applies the chat template embedded in the GGUF (the <|user|> /
# <|assistant|> format the fine-tune was trained with) instead of guessing.
ENTRYPOINT ["/app/llama-server"]
CMD ["--model", "/models/model.q8_0.gguf", "--jinja"]
