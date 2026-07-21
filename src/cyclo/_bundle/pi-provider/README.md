# Cyclo Provider extension for Pi

This package is the consumer edge between Pi and Cyclo's portable Provider
interface. It is an embedded Pi extension, not a component and not a service.

At Pi startup it connects to the Unix socket in `CYCLO_PROVIDER_SOCKET`, calls
`Provider.ListModels`, splits each public `PROVIDER/MODEL` name at the first
slash, and registers the resulting providers and models with Pi. Its custom
`streamSimple` implementation translates Pi context and tool calls to
`Provider.Infer`, then translates the validated response stream back to Pi
events.

The socket mount is the authority. The extension sends no bearer token,
authorization header, API key, URL, or caller metadata. The `apiKey` marker in
the Pi registration only satisfies Pi's local model-availability check; it is
never placed on the Provider RPC.

The adapter fails closed when a model or event needs a capability Pi cannot
represent, including typed extensions, output media, signed native history,
and content-filter/refusal terminal reasons. Costs remain zero in Pi because
the portable catalogue does not publish prices; the gateway retains the
authoritative usage audit.

```sh
npm ci
npm test
```

This package is intentionally not copied into Cyclo's current team image and
does not modify the public CLI yet.
