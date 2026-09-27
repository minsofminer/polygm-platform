"use client";
import { useEffect, useRef } from "react";

/**
 * A poll that cannot overlap itself.
 *
 * Both polling screens scheduled their loader with `setInterval(() => void load(), ms)` — the market book every
 * 2s, the terminal's market detail every 10s. That reads as "every 2 seconds", but it means "start a new request
 * every 2 seconds **whether or not the last one has answered**". `request()` retries with backoff, so a slow or
 * refusing API turns a 2s poll into a stack of in-flight requests, and because responses are not ordered, an
 * older book can land after a newer one and win. The failure mode is worst exactly when the product is least
 * healthy: the tape freezes on stale data while a queue of retries drains.
 *
 * This schedules the next run **after the previous one settles**, so the interval is a floor on the gap between
 * runs rather than a rate of fire, and there is at most one request in flight per poll. The task's own errors are
 * its business (the loaders already branch on `ok` and set their own state), so a rejected promise must not stop
 * the loop — but it must not escape either, because an unhandled rejection in a timer is a console full of noise
 * and no diagnosis.
 *
 * Not covered here, deliberately: pausing while the tab is hidden. That is a behaviour change a reader would want
 * to see measured before it ships (background tabs are cheap on some platforms, throttled on others), and it is
 * recorded as deferred in `plans/react-review.md` rather than smuggled into a fix about overlapping requests.
 */
export function usePoll(task: () => void | Promise<void>, intervalMs: number, enabled = true): void {
  const taskRef = useRef(task);
  // The latest task without re-arming the timer: a screen whose loader depends on state (a market id, a filter)
  // must not restart its poll on every render, and the timer must call the *current* loader, not the first one.
  useEffect(() => {
    taskRef.current = task;
  });

  useEffect(() => {
    if (!enabled) return;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const tick = async () => {
      try {
        await taskRef.current();
      } catch {
        // The loader reports its own failures through state; a poll must survive them and keep polling.
      }
      if (cancelled) return;
      timer = setTimeout(tick, intervalMs);
    };
    void tick();
    return () => {
      cancelled = true;
      if (timer !== undefined) clearTimeout(timer);
    };
  }, [intervalMs, enabled]);
}
