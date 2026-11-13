export const MIN_LENGTH_SECONDS = 5;
export const MAX_LENGTH_SECONDS = 3600;

export function lengthStops() {
  const stops = [];
  for (let seconds = 5; seconds < 60; seconds += 5) stops.push(seconds);
  for (let minutes = 1; minutes <= 60; minutes += 1) stops.push(minutes * 60);
  return stops;
}

export function lengthIndex(seconds) {
  const stops = lengthStops();
  const n = Number(seconds);
  if (!Number.isFinite(n)) return stops.indexOf(300);
  let best = 0;
  let bestDiff = Infinity;
  stops.forEach((stop, index) => {
    const diff = Math.abs(stop - n);
    if (diff < bestDiff) {
      best = index;
      bestDiff = diff;
    }
  });
  return best;
}

export function formatLength(seconds) {
  const n = Math.round(Number(seconds));
  if (!Number.isFinite(n)) return "";
  if (n < 60) return n === 1 ? "1 second" : `${n} seconds`;
  const minutes = Math.max(1, Math.round(n / 60));
  return minutes === 1 ? "1 minute" : `${minutes} minutes`;
}
