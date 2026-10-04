# Optional Jev text review

For the experimental local Ollaya alternative and its manual connection settings,
see [the AI extension guide](README.md#experimental-jev-compatible-text-review).
It uses open local models, not proprietary Jev weights. A saved local connection
overrides the environment selection; saving it disabled also disables this
service. The TypeSafe instructions below apply when no local connection is saved.

The server service `ai.jev.evaluate_event_text` classifies event descriptions or
transcribed text as `factual_observation`, `reported_speech` or `unclear` for human
review. It does not establish that an event happened, identify anyone, infer
intent or danger, authorize actions, or generate pipelines.

The adapter follows the [TypeSafe HTTP contract](https://docs.typesafe.ai/api),
with the [versioned text-only model](https://docs.typesafe.ai/models)
`jev-1.13.0`. Images, audio and video are not accepted. The text is sent in the
request's `state` as untrusted data, separately from the fixed question and rubric.

It is disabled by default. A server operator must explicitly set
`TOPOSYNC_JEV_ENABLED=1` and supply `TYPESAFE_API_KEY` in the server environment.
The trusted caller must independently obtain authorization to send the exact
text to TypeSafe and pass `remote_authorized=True`. Enabling the service or
possessing a key does not establish consent for private event text. No pipeline,
browser endpoint or fallback to this cloud service is enabled automatically.

Each call permits one request, at most 16 KiB of UTF-8 input, at most 64 KiB of
response data, and a ten-second total deadline. Redirects and retries are not
followed. The returned model, question, choice, probabilities and confidence
must pass schema and consistency checks. Failure returns an unavailable result
with `evaluation: null`; a caller must preserve local review rather than infer
a classification. Successful results also have `review_required: true`.
Confidence is provider output, not an independently calibrated accuracy claim.

`tests/test_ai_jev.py` verifies the HTTP contract with synthetic text and a
controlled transport, including disabled/unauthorized calls, authentication and
rate-limit failures, timeouts, oversized or malformed responses, and advisory
results. It does not validate live credentials, provider behavior, linguistic
accuracy or prompt-injection resistance. Those gates remain open until an
explicitly authorized remote evaluation with an independent test set.
