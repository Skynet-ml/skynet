/**
 * Build the `before-quit` handler. The first quit request hides the app, then
 * starts `shutdown`. Every request is held with preventDefault, including a
 * second Cmd+Q or a Dock Quit while the stack stops: a request that is not held
 * lets Electron exit at once and leaves Postgres, job containers and
 * `stack.pids` behind. `shutdown` must end with `app.exit`, which does not emit
 * `before-quit`.
 *
 * `hide` runs first, so the window and the Dock icon go away at once and the
 * stack stops out of sight.
 */
export function holdQuitUntilStopped(
  shutdown: () => void,
  hide: () => void = () => {},
): (event: { preventDefault(): void }) => void {
  let started = false;
  return (event) => {
    event.preventDefault();
    if (started) return;
    started = true;
    hide();
    shutdown();
  };
}

export interface StackStops {
  frontend(): Promise<void>;
  api(): Promise<void>;
  postgres(): Promise<void>;
  sandboxes(): Promise<void>;
}

/**
 * Stop the web server and the API together, then Postgres and the sandbox
 * containers together. Postgres waits for the API: the API's graceful stop can
 * still write job state to the database.
 */
export async function stopServices(stops: StackStops): Promise<void> {
  await Promise.all([stops.frontend(), stops.api()]);
  await Promise.all([stops.postgres(), stops.sandboxes()]);
}
