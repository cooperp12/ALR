# Hardware Requirements

## Minimum

- Modern CPU
- 16 GB RAM recommended
- SSD storage recommended

## Local AI models

ALR supports local Ollama models.

VRAM requirements depend on the selected model:

- Small models (3B-8B): approximately 4-12 GB VRAM depending on quantisation/context.
- 20B class models: commonly require substantially more VRAM or partial CPU offload.

A GPU with more VRAM improves throughput and allows larger context windows.

Configure models in your local Ollama installation rather than committing hardware-specific settings.
