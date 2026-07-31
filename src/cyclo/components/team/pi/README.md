# Cyclo Provider adapter for Pi

This embedded Pi extension is the team-side endpoint of Cyclo's provider
transport. It is not a component and owns no credentials.

At startup it calls `Provider.ListModels` at the canonical DComp target in
`DCOMP_LINK_PROVIDER`, checks the advertised Pi inference-format version, and
registers the returned `PROVIDER/MODEL` entries with Pi.

For inference it serializes exactly one call frame:

```json
{"context": {"messages": []}, "options": {}}
```

The `context` is Pi's context object. `options` contains every JSON inference
option supplied by Pi. Local process values (`signal`), credential controls
(`apiKey`, `headers`, `env`), injected clients, and callback functions do not
enter the payload. Cancellation and deadlines use ConnectRPC call options.

Each streamed response payload is parsed once and pushed directly into Pi's
`AssistantMessageEventStream`. The extension does not validate messages, tool
schemas, signatures, reasoning content, tool arguments, event types, or future
Pi fields. Transport failures become a terminal Pi error event because that is
the contract of Pi's local stream API.

```sh
npm ci
npm test
```

The tests use a real TCP ConnectRPC server and prove that signed
history, unknown fields, and unrestricted JSON Schema constructs such as
`anyOf` cross the boundary unchanged.
