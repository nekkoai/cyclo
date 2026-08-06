import { timestampFromDate } from "@bufbuild/protobuf/wkt";
import { Code, ConnectError } from "@connectrpc/connect";

import { ResourceExhaustionSchema } from "@cyclo/provider/contract";

const EXHAUSTED_MESSAGE = "provider resource exhausted";
const MIN_TIMESTAMP_SECONDS = -62_135_596_800n;
const MAX_TIMESTAMP_SECONDS = 253_402_300_799n;

export function createResourceExhaustedError(retryAt) {
  if (!(retryAt instanceof Date) || !Number.isFinite(retryAt.getTime())) {
    throw new TypeError("retryAt must be a valid Date");
  }
  const timestamp = timestampFromDate(retryAt);
  if (!validTimestamp(timestamp)) {
    throw new RangeError("retryAt is outside the protobuf Timestamp range");
  }
  return new ConnectError(
    EXHAUSTED_MESSAGE,
    Code.ResourceExhausted,
    undefined,
    [{
      desc: ResourceExhaustionSchema,
      value: { retryAt: timestamp },
    }],
  );
}

export function resourceExhaustedRetryAt(error) {
  if (!(error instanceof ConnectError) || error.code !== Code.ResourceExhausted) {
    return undefined;
  }
  let rawDetails;
  try {
    rawDetails = error.details.filter((detail) => {
      if (detail === null || typeof detail !== "object") return false;
      return (
        "desc" in detail ? detail.desc?.typeName : detail.type
      ) === ResourceExhaustionSchema.typeName;
    });
  } catch {
    return undefined;
  }
  if (rawDetails.length !== 1) return undefined;

  let details;
  try {
    details = error.findDetails(ResourceExhaustionSchema);
  } catch {
    return undefined;
  }
  if (details.length !== 1 || !validTimestamp(details[0].retryAt)) return undefined;

  const { seconds, nanos } = details[0].retryAt;
  const retryAt = new Date(Number(seconds) * 1_000 + Math.ceil(nanos / 1_000_000));
  return Number.isFinite(retryAt.getTime()) ? retryAt : undefined;
}

function validTimestamp(value) {
  return value !== undefined
    && typeof value.seconds === "bigint"
    && value.seconds >= MIN_TIMESTAMP_SECONDS
    && value.seconds <= MAX_TIMESTAMP_SECONDS
    && Number.isInteger(value.nanos)
    && value.nanos >= 0
    && value.nanos <= 999_999_999;
}
