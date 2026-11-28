/** A stand-in for next/navigation: a router whose moves do nothing. */
const router = { back: () => {}, forward: () => {}, push: () => {}, replace: () => {} };

/** Return the stub router. */
export function useRouter() {
  return router;
}
