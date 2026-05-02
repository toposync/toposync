# Toposync AI Extension

First-party extension for AI-assisted pipeline operators.

This first phase focuses on image workflows:

- `ai.smart_crop`: locates an object/region from a natural-language description and crops the frame.
- `ai.condition_filter`: evaluates a natural-language visual condition and emits packets only when it matches.

The extension is designed around local-first usage. Ollama is the default provider target, with
`qwen3-vl:30b` as the initial high-quality local vision recommendation. Cloud providers can be
added through provider profiles and explicit fallback chains.
