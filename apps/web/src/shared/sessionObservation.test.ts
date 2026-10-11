import { expect, it } from "vitest";

import { sessionObservationMatches } from "./sessionObservation";

it.each([
  { label: "exact observation", expectedVersion: 5, expectedSequence: 3, version: 5, sequence: 3, matches: true },
  { label: "forward observation", expectedVersion: 5, expectedSequence: 3, version: 9, sequence: 7, matches: true },
  { label: "zero sequence", expectedVersion: 1, expectedSequence: 0, version: 4, sequence: 3, matches: true },
  { label: "version-only advance", expectedVersion: 5, expectedSequence: 3, version: 6, sequence: 3, matches: false },
  { label: "sequence-only advance", expectedVersion: 5, expectedSequence: 3, version: 5, sequence: 4, matches: false },
  { label: "backward observation with equal baseline", expectedVersion: 5, expectedSequence: 3, version: 4, sequence: 2, matches: false },
  { label: "sequence regression", expectedVersion: 5, expectedSequence: 3, version: 6, sequence: 2, matches: false },
  { label: "changed baseline", expectedVersion: 5, expectedSequence: 3, version: 9, sequence: 6, matches: false },
  { label: "both proofs omitted", expectedVersion: 5, expectedSequence: undefined, version: 5, sequence: undefined, matches: true },
  { label: "null proofs are absent", expectedVersion: 5, expectedSequence: null, version: 5, sequence: null, matches: true },
  { label: "missing expected proof at equal version", expectedVersion: 5, expectedSequence: undefined, version: 5, sequence: 3, matches: true },
  { label: "missing current proof at equal version", expectedVersion: 5, expectedSequence: 3, version: 5, sequence: undefined, matches: true },
  { label: "null expected proof at equal version", expectedVersion: 5, expectedSequence: null, version: 5, sequence: 0, matches: true },
  { label: "null current proof at equal version", expectedVersion: 5, expectedSequence: 0, version: 5, sequence: null, matches: true },
  { label: "missing expected proof cannot advance", expectedVersion: 5, expectedSequence: undefined, version: 6, sequence: 4, matches: false },
  { label: "missing current proof cannot advance", expectedVersion: 5, expectedSequence: 3, version: 6, sequence: undefined, matches: false },
  { label: "null expected proof cannot advance", expectedVersion: 5, expectedSequence: null, version: 6, sequence: 4, matches: false },
  { label: "null current proof cannot advance", expectedVersion: 5, expectedSequence: 3, version: 6, sequence: null, matches: false },
  { label: "absent proofs cannot regress", expectedVersion: 5, expectedSequence: undefined, version: 4, sequence: null, matches: false },
  { label: "safe integer upper boundary", expectedVersion: 9007199254740989, expectedSequence: 9007199254740987, version: 9007199254740991, sequence: 9007199254740989, matches: true },
])("checks ordered approval freshness for $label", ({ expectedVersion, expectedSequence, version, sequence, matches }) => {
  expect(sessionObservationMatches(expectedVersion, expectedSequence, version, sequence)).toBe(matches);
});

it.each([undefined, null, true, false, 0, -1, 5.5, Number.NaN, Number.POSITIVE_INFINITY,
  Number.NEGATIVE_INFINITY, Number.MAX_SAFE_INTEGER + 1, "5", {}, []])(
  "rejects invalid version %s on either side even without proof",
  (version) => {
    expect(sessionObservationMatches(version, undefined, 5, undefined)).toBe(false);
    expect(sessionObservationMatches(5, undefined, version, undefined)).toBe(false);
    expect(sessionObservationMatches(version, 3, 5, 3)).toBe(false);
    expect(sessionObservationMatches(5, 3, version, 3)).toBe(false);
  },
);

it.each([true, false, -1, 1.5, 5, 6, Number.NaN, Number.POSITIVE_INFINITY,
  Number.NEGATIVE_INFINITY, Number.MAX_SAFE_INTEGER + 1, "3", {}, []])(
  "rejects invalid supplied sequence %s on either side before absent-proof fallback",
  (sequence) => {
    expect(sessionObservationMatches(5, sequence, 5, undefined)).toBe(false);
    expect(sessionObservationMatches(5, undefined, 5, sequence)).toBe(false);
    expect(sessionObservationMatches(5, sequence, 5, 3)).toBe(false);
    expect(sessionObservationMatches(5, 3, 5, sequence)).toBe(false);
  },
);

it("bounds each sequence by its own version", () => {
  expect(sessionObservationMatches(3, 3, 5, 4)).toBe(false);
  expect(sessionObservationMatches(3, 2, 5, 5)).toBe(false);
  expect(sessionObservationMatches(3, 2, 5, 4)).toBe(true);
});
