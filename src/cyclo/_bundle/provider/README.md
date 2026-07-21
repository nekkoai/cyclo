# Cyclo Provider interface

`@cyclo/provider` defines the portable interface between Cyclo provider
components. It is a Protobuf contract served with ConnectRPC over HTTP/1.1.
It is deliberately separate from `@cyclo/component`: component health and
model inference can evolve independently.

This package is an interface, not a provider implementation and not a proxy
for a native HTTP API. It contains no URL, header, credential, Docker, routing,
retry, or discovery configuration.

## Interface

```proto
service Provider {
  rpc ListModels(ListModelsRequest) returns (ListModelsResponse);
  rpc Infer(InferRequest) returns (stream InferResponse);
}
```

`ListModels` returns the exact model IDs accepted by that component and the
portable capabilities of each model. IDs are opaque and local to that
Provider. A component that transforms or combines upstream models publishes
its own catalogue.

`Infer` takes:

- one model ID and one text instruction;
- an ordered history of user/assistant messages, reasoning summaries, tool
  calls, tool results, and typed provider state;
- inline text or media content;
- caller-executed function schemas;
- presence-aware portable generation controls; and
- explicitly advertised typed extensions.

Media is inline bytes plus an IANA media type. Remote URLs and provider file
IDs are intentionally absent. Function schemas and completed arguments are
typed as JSON objects rather than opaque request bodies.

## Stream

One successful inference has this state machine:

```text
Started
  ItemStarted(0)  ─┬─ ItemDelta(0) ... ─ ItemFinished(0)
  ItemStarted(1)  ─┴─ ItemDelta(1) ... ─ ItemFinished(1)
Finished(reason, usage?)
```

Item indexes start at zero and increase without gaps. Deltas for open items may
interleave, which represents parallel tool calls without losing their
identity. Every item closes exactly once. `Started` is first and `Finished` is
last; EOF without `Finished` is failure.

Text, reasoning summaries, tool calls, media, and typed native items use the
same lifecycle. Tool argument deltas provide low latency, while the parsed
object on `ItemFinished` is authoritative. A reasoning-summary item is text a
provider intentionally exposes—it is never hidden chain-of-thought. Typed
native items are atomic; their extension contract may not invent an unstated
delta merge operation.

When building the next request, one completed output item becomes one ordered
`InputItem`; reasoning summaries remain distinct from ordinary assistant text.
Item-bound extensions copy to that input item's `extensions`, so signatures and
other provider state stay attached to the exact text, media, reasoning summary,
or tool call they authenticate. A native item with no portable representation
uses the `InputItem.extension` variant instead.

Connect errors are the only failure channel:

- unknown model: `NOT_FOUND`;
- malformed input or an unadvertised capability: `INVALID_ARGUMENT`, before
  `Started`;
- unavailable dependency: `UNAVAILABLE`;
- caller cancellation or deadline: the corresponding Connect status.

Components propagate cancellation and deadlines upstream. They must not retry
after emitting any output because that would duplicate a partial response.
`Started.model` is always the exact provider-local model requested by the
caller. A multiplexer records a selected inner route, if useful, in a typed
extension rather than leaking an upstream namespace into its public catalogue.

The package exports `validateInferStream()` from `@cyclo/provider/protocol`.
Intermediaries wrap upstream streams with it so clean truncation, missing or
duplicate item transitions, incompatible deltas, and events after `Finished`
become `DATA_LOSS` instead of looking like successful EOF.

## Request rules

An implementation validates the complete request before emitting `Started`:

- model IDs must occur in the current catalogue;
- tool names and tool-call IDs are non-empty and unique;
- every tool result matches exactly one earlier unmatched call;
- `SPECIFIC` names a declared tool;
- an absent `ToolChoice` means `AUTO`, while a present `UNSPECIFIED` mode is
  invalid; and
- message roles, content, media types, schemas, controls, and extensions must
  be valid for the selected model.

`function_tools` promises portable function calling, not every JSON Schema
dialect. Likewise, a modality does not promise every media type. An adapter
accepts the common subset it implements and rejects an unsupported schema or
media type with `INVALID_ARGUMENT`; it never silently weakens one.

## Composition

The generic component declaration says only what a program provides and needs:

```text
component passthrough
provide cyclo.component.v1.Component
provide cyclo.provider.v1.Provider
require upstream cyclo.provider.v1.Provider
```

A multiplexer simply has more named requirements:

```text
component multiplexer
provide cyclo.component.v1.Component
provide cyclo.provider.v1.Provider
require primary cyclo.provider.v1.Provider
require secondary cyclo.provider.v1.Provider
```

An assembly layer binds `upstream`, `primary`, and `secondary` to component
endpoints. Neither interface package performs that binding.

## Native API boundary

The portable core has direct equivalents in OpenAI Responses, Anthropic
Messages, and Gemini GenerateContent:

| Cyclo | OpenAI | Anthropic | Gemini |
|---|---|---|---|
| instructions | `instructions` | `system` | `systemInstruction` |
| message/content | input items and content parts | messages and content blocks | contents and parts |
| function tool | function tool | tool with `input_schema` | function declaration |
| tool call/result | function call/output | `tool_use`/`tool_result` | `functionCall`/`functionResponse` |
| indexed stream item | output item/content events | content-block events | streamed candidate parts |
| finish and usage | response status and usage | stop reason and usage | finish reason and usage metadata |

Provider-hosted tools, persisted conversations, remote files, prompt-cache
controls, safety and grounding metadata, citations, multiple candidates,
structured-output dialects, and opaque reasoning state are not falsely
normalized. They use versioned Protobuf messages in `google.protobuf.Any`, and
their fully-qualified message types must appear in the selected model's
`extension_types`. The final path segment of `Any.type_url` must match the
advertised fully-qualified message name. Each extension's own versioned
contract declares whether it is request or response data and where it may be
attached. Unknown or misplaced request extensions are rejected rather than
ignored.

Usage is optional because some upstreams cannot report it. When present,
`cached_input_tokens` is a subset of input, `reasoning_tokens` is a subset of
output, and `total_tokens = input_tokens + output_tokens`. A pass-through
forwards usage unchanged, a multiplexer reports the selected call, and a fusion
sums every upstream inference it performed for the returned result.

## Build and test

Node.js 20 or newer is required for the current generated ECMAScript bindings.

```sh
npm ci
npm test
```

The tests lint and regenerate the contract, verify its descriptors, compose it
with the base Component interface, and make a real incremental two-hop Connect
call over Unix-domain sockets. They also verify interleaved tool items,
cancellation propagation, malformed and truncated streams, capability errors,
and failures both before and after the first streamed event.

This package is intentionally not connected to Cyclo's current CLI or legacy
provider runtime yet. The sibling `../gateway` program is its first standalone
implementation.
