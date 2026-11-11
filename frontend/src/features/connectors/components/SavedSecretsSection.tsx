"use client";

import * as React from "react";

import { deleteSavedSecret, listSavedSecrets, saveSecret, type SavedSecret } from "@/shared/lib/api";
import { formatMsg, msg } from "@/shared/lib/messages";
import { Key, Trash } from "@/shared/ui/icons";
import { Button } from "@/shared/ui/primitives/button";
import { Input } from "@/shared/ui/primitives/input";
import { Label } from "@/shared/ui/primitives/label";

import { SECRET_NAME, secretNameFromSearch, withoutSecretParam } from "../lib/secret-prefill";

/**
 * Settings → Connectors → Saved secrets: the secrets saved on the account, by
 * name, with a form to save or replace one (SKY-641). A remote MCP
 * `request_secret` link fills in the name. A saved value never comes back
 * from the server, so the list shows names only.
 */
export function SavedSecretsSection() {
  const [secrets, setSecrets] = React.useState<SavedSecret[] | null>(null);
  const [loadFailed, setLoadFailed] = React.useState(false);
  const [name, setName] = React.useState("");
  const [value, setValue] = React.useState("");
  const [busy, setBusy] = React.useState(false);
  const [notice, setNotice] = React.useState<"saved" | "save_failed" | "delete_failed" | null>(null);
  const sectionRef = React.useRef<HTMLElement>(null);
  const valueRef = React.useRef<HTMLInputElement>(null);

  const load = React.useCallback(() => {
    listSavedSecrets()
      .then((res) => {
        setSecrets(res.secrets);
        setLoadFailed(false);
      })
      .catch(() => setLoadFailed(true));
  }, []);

  React.useEffect(() => {
    load();
    const prefill = secretNameFromSearch(window.location.search);
    if (!prefill) return;
    setName(prefill);
    window.history.replaceState(window.history.state, "", withoutSecretParam(window.location.href));
    sectionRef.current?.scrollIntoView({ block: "center" });
    valueRef.current?.focus();
  }, [load]);

  const validName = SECRET_NAME.test(name);

  const save = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!validName || !value) return;
    setBusy(true);
    setNotice(null);
    try {
      await saveSecret(name, value);
      setValue("");
      setName("");
      setNotice("saved");
      load();
    } catch {
      // The server's error text stays out of the page; it may name the input.
      setNotice("save_failed");
    } finally {
      setBusy(false);
    }
  };

  const remove = async (secret: SavedSecret) => {
    setNotice(null);
    try {
      await deleteSavedSecret(secret.id);
      load();
    } catch {
      setNotice("delete_failed");
    }
  };

  return (
    <section ref={sectionRef} data-setup="saved-secrets" className="flex flex-col gap-2.5">
      <h4 className="flex items-center gap-1.5 text-[0.6875rem] font-semibold uppercase tracking-widest text-muted-foreground">
        <Key className="size-3.5 shrink-0" aria-hidden="true" />
        {msg("settings.secrets.title")}
      </h4>
      <p className="text-xs text-muted-foreground">{msg("settings.secrets.subtitle")}</p>

      {loadFailed ? (
        <p className="text-sm text-muted-foreground">{msg("settings.secrets.load_failed")}</p>
      ) : secrets && secrets.length === 0 ? (
        <p className="text-sm text-muted-foreground">{msg("settings.secrets.empty")}</p>
      ) : (
        <ul className="flex flex-col divide-y divide-border/50 rounded-lg border border-border/50">
          {(secrets ?? []).map((secret) => (
            <li key={secret.id} className="flex items-center justify-between gap-3 px-3 py-2">
              <code dir="ltr" className="min-w-0 truncate font-mono text-sm">
                {secret.name}
              </code>
              <Button
                type="button"
                variant="ghost"
                size="icon"
                onClick={() => remove(secret)}
                aria-label={formatMsg("settings.secrets.delete_aria", { name: secret.name })}
              >
                <Trash aria-hidden="true" />
              </Button>
            </li>
          ))}
        </ul>
      )}

      <form onSubmit={save} autoComplete="off" className="flex flex-col gap-2 rounded-lg border border-border/50 p-3">
        <div className="grid gap-2 sm:grid-cols-2">
          <div className="flex flex-col gap-1">
            <Label htmlFor="saved-secret-name">{msg("settings.secrets.name_label")}</Label>
            <Input
              id="saved-secret-name"
              dir="ltr"
              spellCheck={false}
              value={name}
              onChange={(event) => setName(event.target.value)}
              aria-invalid={(name !== "" && !validName) || undefined}
              placeholder="MAPS_KEY"
              className="font-mono"
            />
          </div>
          <div className="flex flex-col gap-1">
            <Label htmlFor="saved-secret-value">{msg("settings.secrets.value_label")}</Label>
            <Input
              id="saved-secret-value"
              ref={valueRef}
              type="password"
              dir="ltr"
              autoComplete="off"
              spellCheck={false}
              value={value}
              onChange={(event) => setValue(event.target.value)}
              className="font-mono"
            />
          </div>
        </div>
        {name !== "" && !validName && (
          <p className="text-xs text-destructive">{msg("submit.blackbox.repo.validation.secret_name")}</p>
        )}
        {notice && (
          <p role="status" className={notice === "saved" ? "text-xs text-muted-foreground" : "text-xs text-destructive"}>
            {msg(`settings.secrets.${notice}`)}
          </p>
        )}
        <Button type="submit" disabled={busy || !validName || !value} className="self-end">
          {msg("settings.secrets.save")}
        </Button>
      </form>
    </section>
  );
}
