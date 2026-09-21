"""One-shot local Qwen worker. It never imports or executes generated Python."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from .advisor import MAX_FINDINGS, MAX_QUESTION_CHARS, SYSTEM_PROMPT

MAX_INPUT_TOKENS = 6000
MAX_OUTPUT_TOKENS = 260


def generate(request: dict) -> dict:
    import torch
    import transformers
    from transformers import AutoModelForCausalLM, AutoTokenizer

    if not torch.cuda.is_available():
        raise RuntimeError("A CUDA GPU is required for this local model; use the rules mode on CPU.")
    question, findings = request["question"], request["findings"]
    if not isinstance(question, str) or len(question) > MAX_QUESTION_CHARS:
        raise ValueError("Question exceeds the bounded input contract")
    if not isinstance(findings, list) or not 1 <= len(findings) <= MAX_FINDINGS:
        raise ValueError("Finding summaries exceed the bounded input contract")
    path = request["model_path"]
    if not Path(path).is_dir():
        raise FileNotFoundError("Local model directory is unavailable")
    torch.set_num_threads(2)
    started = time.perf_counter()
    tokenizer = AutoTokenizer.from_pretrained(path, local_files_only=True)
    messages = [{"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps(
                    {"question": question, "finding_summaries": findings}, ensure_ascii=False)}]
    prompt = tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
    inputs = tokenizer(prompt, return_tensors="pt")
    input_tokens = inputs["input_ids"].shape[-1]
    if input_tokens > MAX_INPUT_TOKENS:
        raise ValueError("Prompt exceeds the input token budget; use a narrower investigation")
    model = AutoModelForCausalLM.from_pretrained(
        path, dtype=torch.bfloat16, local_files_only=True,
    ).to("cuda").eval()
    load_seconds = time.perf_counter() - started
    inputs = inputs.to("cuda")
    started = time.perf_counter()
    with torch.inference_mode():
        output = model.generate(**inputs, max_new_tokens=MAX_OUTPUT_TOKENS, do_sample=False,
                                pad_token_id=tokenizer.eos_token_id)
    torch.cuda.synchronize()
    raw = tokenizer.decode(output[0, input_tokens:], skip_special_tokens=True)
    return {"ok": True, "raw_response": raw, "input_tokens": input_tokens,
            "output_tokens": output.shape[-1] - input_tokens,
            "load_seconds": round(load_seconds, 4),
            "generation_seconds": round(time.perf_counter() - started, 4),
            "python": sys.version, "torch": torch.__version__, "transformers": transformers.__version__}


def main() -> int:
    if len(sys.argv) != 3:
        return 2
    request_path, result_path = map(Path, sys.argv[1:])
    try:
        if request_path.stat().st_size > 128_000:
            raise ValueError("Request exceeds the byte limit")
        result = generate(json.loads(request_path.read_text(encoding="utf-8")))
    except Exception as exc:
        # Only return a bounded diagnostic; no traceback, source data, or environment dump.
        result = {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:240]}"}
    result_path.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
