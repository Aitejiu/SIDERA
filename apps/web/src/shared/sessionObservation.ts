export function sessionObservationMatches(
  expectedVersion: unknown,
  expectedLastSequence: unknown,
  currentVersion: unknown,
  currentLastSequence: unknown,
): boolean {
  if (!isVersion(expectedVersion) || !isVersion(currentVersion)) return false;
  if (expectedLastSequence == null || currentLastSequence == null) {
    return currentVersion === expectedVersion
      && (expectedLastSequence == null || isSequence(expectedLastSequence, expectedVersion))
      && (currentLastSequence == null || isSequence(currentLastSequence, currentVersion));
  }
  if (!isSequence(expectedLastSequence, expectedVersion) || !isSequence(currentLastSequence, currentVersion)) return false;

  return currentVersion >= expectedVersion
    && currentLastSequence >= expectedLastSequence
    && currentVersion - expectedVersion === currentLastSequence - expectedLastSequence;
}

function isVersion(value: unknown): value is number {
  return typeof value === "number" && Number.isSafeInteger(value) && value > 0;
}

function isSequence(value: unknown, version: number): value is number {
  return typeof value === "number" && Number.isSafeInteger(value) && value >= 0 && value < version;
}
