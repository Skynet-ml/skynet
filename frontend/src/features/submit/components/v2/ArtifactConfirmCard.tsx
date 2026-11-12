"use client";

import { msg, type MessageKey } from "@/shared/lib/messages";
import { getActiveMessages } from "@/shared/lib/runtime-messages";

type MessageParams = Record<string, string | number>;

/**
 * A wizard v2 string, taking its plainer `<key>.guided` sibling for guided
 * users when the catalog has one.
 */
export function v2Msg(key: string, guided: boolean, params?: MessageParams): string {
  const guidedKey = `${key}.guided`;
  const chosen = guided && getActiveMessages()[guidedKey as MessageKey] ? guidedKey : key;
  return msg(chosen as MessageKey, params);
}
