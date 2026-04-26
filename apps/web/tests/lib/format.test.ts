import { describe, expect, it } from "vitest";

import {
  formatBytes,
  formatRelativeTime,
  formatTokens,
  formatUsd,
} from "@/lib/format";

describe("formatUsd", () => {
  it("formats a small decimal to two digits", () => {
    expect(formatUsd(0.0525)).toBe("$0.05");
  });
  it("formats a decimal string input", () => {
    expect(formatUsd("1.2345")).toBe("$1.23");
  });
  it("defaults to $0.00 for null/undefined/non-finite", () => {
    expect(formatUsd(null)).toBe("$0.00");
    expect(formatUsd(undefined)).toBe("$0.00");
    expect(formatUsd(Number.NaN)).toBe("$0.00");
  });
});

describe("formatTokens", () => {
  it("renders large counts in compact notation", () => {
    expect(formatTokens(142000)).toBe("142K tokens");
  });
  it("renders small counts without suffix", () => {
    expect(formatTokens(750)).toBe("750 tokens");
  });
  it("renders millions compactly", () => {
    expect(formatTokens(2_300_000)).toBe("2.3M tokens");
  });
  it("renders 0 tokens for null/invalid input", () => {
    expect(formatTokens(null)).toBe("0 tokens");
    expect(formatTokens(-1)).toBe("0 tokens");
  });
});

describe("formatBytes", () => {
  it("renders bytes for sub-KiB values", () => {
    expect(formatBytes(0)).toBe("0 B");
    expect(formatBytes(500)).toBe("500 B");
    expect(formatBytes(1023)).toBe("1023 B");
  });
  it("renders KiB for the 1024-byte threshold", () => {
    expect(formatBytes(1024)).toBe("1.0 KiB");
  });
  it("renders MiB for ~1.5MB", () => {
    expect(formatBytes(1_500_000)).toBe("1.4 MiB");
  });
  it("renders GiB for large casebook-sized PDFs", () => {
    expect(formatBytes(2 * 1024 * 1024 * 1024)).toBe("2.0 GiB");
  });
  it("returns 0 B for null/undefined/non-finite/negative", () => {
    expect(formatBytes(null)).toBe("0 B");
    expect(formatBytes(undefined)).toBe("0 B");
    expect(formatBytes(Number.NaN)).toBe("0 B");
    expect(formatBytes(-1)).toBe("0 B");
  });
});

describe("formatRelativeTime", () => {
  const now = new Date("2026-04-20T12:00:00.000Z");

  it("returns 'just now' for very recent timestamps", () => {
    const iso = new Date(now.getTime() - 2_000).toISOString();
    expect(formatRelativeTime(iso, now)).toBe("just now");
  });
  it("renders minutes", () => {
    const iso = new Date(now.getTime() - 2 * 60_000).toISOString();
    expect(formatRelativeTime(iso, now)).toBe("2m ago");
  });
  it("renders hours", () => {
    const iso = new Date(now.getTime() - 3 * 3600_000).toISOString();
    expect(formatRelativeTime(iso, now)).toBe("3h ago");
  });
  it("returns empty for unparseable input", () => {
    expect(formatRelativeTime("not-a-date", now)).toBe("");
    expect(formatRelativeTime(null, now)).toBe("");
  });
});
