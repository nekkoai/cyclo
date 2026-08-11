function memberIds(value) {
  if (
    !Array.isArray(value)
    || value.length < 2
    || value.some((item) => typeof item !== "string" || item.length === 0)
    || new Set(value).size !== value.length
  ) {
    throw new TypeError("pool members must be at least two distinct non-empty strings");
  }
  return Object.freeze([...value]);
}


function timestamp(value, label) {
  const milliseconds = value instanceof Date ? value.getTime() : value;
  if (
    !Number.isSafeInteger(milliseconds)
    || milliseconds < 0
    || !Number.isFinite(new Date(milliseconds).getTime())
  ) {
    throw new TypeError(`${label} must be a valid absolute timestamp`);
  }
  return milliseconds;
}


function eligibleMemberIds(value, pool) {
  if (value === undefined) return pool.members;
  if (
    !Array.isArray(value)
    || value.length === 0
    || value.some((item) => typeof item !== "string" || !pool.memberSet.has(item))
    || new Set(value).size !== value.length
  ) {
    throw new TypeError(
      "eligible pool members must be distinct members of the configured pool",
    );
  }
  return Object.freeze([...value]);
}


class PoolAttempt {
  constructor(pool, eligibleMembers) {
    this.pool = pool;
    this.eligibleMembers = eligibleMembers;
    this.attempted = new Set();
    this.selected = undefined;
  }

  next(now = this.pool.now()) {
    if (this.selected !== undefined) {
      throw new Error("selected member must be marked exhausted before selecting another");
    }
    const nowMs = timestamp(now, "current time");
    const memberModelId = this.pool.select(
      this.eligibleMembers,
      this.attempted,
      nowMs,
    );
    if (memberModelId !== undefined) {
      this.attempted.add(memberModelId);
      this.selected = memberModelId;
      return Object.freeze({ memberModelId });
    }

    const retryAtMs = this.pool.earliestRetry(this.eligibleMembers, this.attempted);
    if (retryAtMs === undefined) {
      throw new Error("pool attempt has no selectable or exhausted member");
    }
    return Object.freeze({ retryAt: new Date(retryAtMs) });
  }

  markExhausted(retryAt) {
    if (this.selected === undefined) {
      throw new Error("no selected member to mark exhausted");
    }
    this.pool.cool(this.selected, timestamp(retryAt, "retry time"));
    this.selected = undefined;
  }
}


export class PoolScheduler {
  constructor(members, { now = Date.now } = {}) {
    this.members = memberIds(members);
    this.memberSet = new Set(this.members);
    if (typeof now !== "function") throw new TypeError("pool clock must be a function");
    this.now = now;
    this.cursor = 0;
    this.cooldowns = new Map(this.members.map((member) => [member, 0]));
  }

  begin(eligibleMembers) {
    return new PoolAttempt(this, eligibleMemberIds(eligibleMembers, this));
  }

  select(eligibleMembers, attempted, nowMs) {
    const eligible = new Set(eligibleMembers);
    for (let offset = 0; offset < this.members.length; offset += 1) {
      const position = (this.cursor + offset) % this.members.length;
      const member = this.members[position];
      if (
        !eligible.has(member)
        || attempted.has(member)
        || this.cooldowns.get(member) > nowMs
      ) continue;
      this.cursor = (position + 1) % this.members.length;
      return member;
    }
    return undefined;
  }

  cool(member, retryAtMs) {
    this.cooldowns.set(member, Math.max(this.cooldowns.get(member), retryAtMs));
  }

  earliestRetry(eligibleMembers, attempted) {
    const eligible = new Set(eligibleMembers);
    let earliest;
    for (const member of this.members) {
      if (!eligible.has(member)) continue;
      if (!attempted.has(member) && this.cooldowns.get(member) === 0) continue;
      const retryAt = this.cooldowns.get(member);
      earliest = earliest === undefined ? retryAt : Math.min(earliest, retryAt);
    }
    return earliest;
  }
}
