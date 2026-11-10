/** Require an explicit confirmation after the account's password requirement is known. */
export function accountDeletionConfirmed(
  confirmation: string,
  hasPassword: boolean | null,
  password: string,
): boolean {
  return confirmation === "DELETE" &&
    hasPassword !== null &&
    (!hasPassword || password.length > 0);
}
