import assert from "node:assert/strict";
import test from "node:test";

import { Code, ConnectError } from "@connectrpc/connect";
import { FinishReason } from "@cyclo/provider/contract";
import { validateInferStream } from "@cyclo/provider/protocol";

test("accepts a complete stream and preserves object identity", async () => {
  const input = [started(), finished()];
  const output = await collect(validateInferStream(events(input), { model: "model" }));
  assert.deepEqual(output, input);
  assert.equal(output[0], input[0]);
});

test("rejects malformed clean streams as DATA_LOSS", async () => {
  const cases = [
    {
      name: "empty stream",
      input: [],
      message: /before Started/u,
    },
    {
      name: "truncated stream",
      input: [started()],
      message: /before Finished/u,
    },
    {
      name: "item before Started",
      input: [textStarted(0)],
      message: /Started must be the first/u,
    },
    {
      name: "wrong public model",
      input: [
        {
          event: {
            case: "started",
            value: { responseId: "response", model: "inner" },
          },
        },
      ],
      message: /does not match the requested model/u,
    },
    {
      name: "index gap",
      input: [started(), textStarted(1)],
      message: /expected item index 0/u,
    },
    {
      name: "wrong delta type",
      input: [started(), textStarted(0), toolDelta(0)],
      message: /tool-argument delta does not belong/u,
    },
    {
      name: "unclosed item",
      input: [started(), textStarted(0), finished()],
      message: /Finished has open items/u,
    },
    {
      name: "event after Finished",
      input: [started(), finished(), textStarted(0)],
      message: /event follows Finished/u,
    },
    {
      name: "inconsistent usage",
      input: [
        started(),
        {
          event: {
            case: "finished",
            value: {
              reason: FinishReason.STOP,
              usage: { inputTokens: 2n, outputTokens: 3n, totalTokens: 6n },
            },
          },
        },
      ],
      message: /total token count is inconsistent/u,
    },
    {
      name: "unknown finish reason",
      input: [started(), {
        event: { case: "finished", value: { reason: 99 } },
      }],
      message: /invalid reason/u,
    },
  ];

  for (const fixture of cases) {
    await assert.rejects(
      collect(validateInferStream(events(fixture.input), { model: "model" })),
      (error) =>
        error instanceof ConnectError &&
        error.code === Code.DataLoss &&
        fixture.message.test(error.message),
      fixture.name,
    );
  }
});

test("does not replace an explicit upstream Connect error", async () => {
  const unavailable = new ConnectError("upstream unavailable", Code.Unavailable);
  async function* failing() {
    yield started();
    throw unavailable;
  }

  await assert.rejects(
    collect(validateInferStream(failing(), { model: "model" })),
    (error) => error === unavailable,
  );
});

test("does not expose Finished until upstream EOF is confirmed", async () => {
  const iterator = validateInferStream(
    events([started(), finished(), textStarted(0)]),
    { model: "model" },
  )[Symbol.asyncIterator]();

  const first = await iterator.next();
  assert.equal(first.value.event.case, "started");
  await assert.rejects(
    iterator.next(),
    (error) =>
      error instanceof ConnectError &&
      error.code === Code.DataLoss &&
      /event follows Finished/u.test(error.message),
  );
});

test("does not expose Finished when upstream errors afterward", async () => {
  const unavailable = new ConnectError("late failure", Code.Unavailable);
  async function* failing() {
    yield started();
    yield finished();
    throw unavailable;
  }
  const iterator = validateInferStream(failing(), { model: "model" })[
    Symbol.asyncIterator
  ]();

  const first = await iterator.next();
  assert.equal(first.value.event.case, "started");
  await assert.rejects(iterator.next(), (error) => error === unavailable);
});

function started() {
  return {
    event: {
      case: "started",
      value: { responseId: "response", model: "model" },
    },
  };
}

function textStarted(index) {
  return {
    event: {
      case: "itemStarted",
      value: { index, item: { case: "text", value: {} } },
    },
  };
}

function toolDelta(index) {
  return {
    event: {
      case: "itemDelta",
      value: {
        index,
        delta: { case: "toolArgumentsJson", value: "{}" },
      },
    },
  };
}

function finished() {
  return {
    event: {
      case: "finished",
      value: { reason: FinishReason.STOP },
    },
  };
}

async function* events(values) {
  yield* values;
}

async function collect(values) {
  const result = [];
  for await (const value of values) result.push(value);
  return result;
}
