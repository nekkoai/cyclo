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
enter the payload. Operator cancellation uses the ConnectRPC signal. `Infer`
has no absolute RPC deadline: Pi's native-provider timeout is not a timeout for
the complete component pipeline, and the gateway owns its upstream network
timeout. Health and catalogue operations keep their own bounded deadlines.

Each streamed response payload is parsed once and pushed directly into Pi's
`AssistantMessageEventStream`. The extension does not validate messages, tool
schemas, signatures, reasoning content, tool arguments, event types, or future
Pi fields. A typed pre-stream `RESOURCE_EXHAUSTED` response is the one
non-terminal condition: the extension waits cancellably until its `retry_at`
time and retries the same request. Intermediate providers therefore remain
free to select another route before exhaustion reaches the team. All other
transport failures, and any failure after a response has arrived, become a
terminal Pi event.

```sh
npm ci
npm test
```

The tests use a real TCP ConnectRPC server and prove that signed
history, unknown fields, and unrestricted JSON Schema constructs such as
`anyOf` cross the boundary unchanged.
